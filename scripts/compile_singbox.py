#!/usr/bin/env python3
"""
ProxyRules — Sing-box 规则编译脚本
=====================================
下载 Sing-box 核心，将 output/*.txt 文本规则编译为:
  1. 符合 sing-box 格式的 JSON 规则集 (中间格式)
  2. .srs 二进制规则集 (sing-box 专有高效格式)

编译映射:
  direct_domain.txt  → direct_domain.srs  (domain_suffix)
  direct_ip.txt      → direct_ip.srs      (ip_cidr)
  private_ip.txt     → private_ip.srs     (ip_cidr)
  private_domain.txt → private_domain.srs (domain_suffix)
  reject_domain.txt  → reject_domain.srs  (domain_suffix)
  reject_ip.txt      → reject_ip.srs      (ip_cidr)
  no_cn_domain.txt   → no_cn_domain.srs   (domain_suffix)

Sing-box 命令:
  sing-box rule-set compile --output output.srs input.json
"""

import os
import sys
import json
import subprocess
import platform
import stat
import shutil
import tarfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

TZ_BEIJING = timezone(timedelta(hours=8))

BASE_DIR   = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
BUILD_DIR  = BASE_DIR / "build" / "sing-box"
CACHE_DIR  = BASE_DIR / ".cache"
TEMP_DIR   = CACHE_DIR / "singbox_json"

# Sing-box release URL
SINGBOX_REPO = "https://github.com/SagerNet/sing-box/releases"
SINGBOX_VERSION = "1.10.1"

# 架构 → sing-box release asset 后缀
ARCH_MAP = {
    ("linux", "x86_64"):  "sing-box-{version}-linux-amd64.tar.gz",
    ("linux", "aarch64"): "sing-box-{version}-linux-arm64.tar.gz",
    ("darwin", "x86_64"): "sing-box-{version}-darwin-amd64.tar.gz",
    ("darwin", "arm64"):  "sing-box-{version}-darwin-arm64.tar.gz",
}

# 编译任务定义: (输入文件名, 规则类型, 输出文件名)
COMPILE_TASKS = [
    ("direct_domain.txt",  "domain", "direct_domain"),
    ("direct_ip.txt",      "ipcidr", "direct_ip"),
    ("private_ip.txt",     "ipcidr", "private_ip"),
    ("private_domain.txt", "domain", "private_domain"),
    ("reject_domain.txt",  "domain", "reject_domain"),
    ("reject_ip.txt",      "ipcidr", "reject_ip"),
    ("no_cn_domain.txt",   "domain", "no_cn_domain"),
]


def detect_platform() -> tuple:
    """返回 (os_name, arch)"""
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    # 统一架构名
    arch_map = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "aarch64": "aarch64", "arm64": "arm64",
    }
    arch = arch_map.get(machine, machine)
    return sys_name, arch


def get_singbox_path() -> Optional[Path]:
    """
    获取 Sing-box 二进制路径
    1. 检查系统 PATH 中的 sing-box
    2. 检查缓存目录
    3. 下载并解压到缓存目录
    """
    # 1. PATH 中查找
    result = subprocess.run(["which", "sing-box"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        path = Path(result.stdout.strip())
        if path.exists():
            print(f"  使用系统 sing-box: {path}")
            return path

    # 2. 缓存目录
    sys_name, arch = detect_platform()
    cache_key = f"{sys_name}-{arch}-{SINGBOX_VERSION}"
    cached_bin = CACHE_DIR / f"sing-box-{cache_key}"

    if cached_bin.exists():
        print(f"  使用缓存 sing-box: {cached_bin}")
        return cached_bin

    # 3. 下载
    return download_singbox(sys_name, arch, cached_bin)


def download_singbox(sys_name: str, arch: str, dest: Path) -> Optional[Path]:
    """下载并解压 Sing-box 二进制到缓存目录"""
    key = (sys_name, arch)
    if key not in ARCH_MAP:
        print(f"  [ERROR] 不支持的平台: {sys_name}/{arch}")
        print(f"  支持的平台: {list(ARCH_MAP.keys())}")
        return None

    asset_pattern = ARCH_MAP[key].format(version=SINGBOX_VERSION)
    download_url = f"{SINGBOX_REPO}/download/v{SINGBOX_VERSION}/{asset_pattern}"

    print(f"  下载 sing-box {SINGBOX_VERSION}: {download_url}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = CACHE_DIR / asset_pattern

    try:
        resp = requests.get(download_url, stream=True, timeout=120)
        resp.raise_for_status()

        with open(tar_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        print(f"  下载完成: {tar_path.stat().st_size / 1024 / 1024:.1f} MB")
    except Exception as e:
        print(f"  [ERROR] 下载失败: {e}")
        return None

    # 解开 tar.gz 并单独提取 sing-box 二进制
    try:
        print(f"  正在解压并提取 sing-box 二进制...")
        dest.parent.mkdir(parents=True, exist_ok=True)
        
        with tarfile.open(tar_path, "r:gz") as tar:
            found = False
            for member in tar.getmembers():
                # 寻找压缩包中的 sing-box 可执行程序
                if member.name.endswith("/sing-box") and member.isfile():
                    f_extracted = tar.extractfile(member)
                    if f_extracted:
                        with open(dest, "wb") as out_f:
                            out_f.write(f_extracted.read())
                        found = True
                        break
            
            if not found:
                print(f"  [ERROR] 未能在压缩包内找到 sing-box 二进制文件。")
                return None

        # 设置可执行权限
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  提取成功: {dest}")
    except Exception as e:
        print(f"  [ERROR] 解压提取失败: {e}")
        return None
    finally:
        # 清理 tar 压缩文件
        tar_path.unlink(missing_ok=True)

    return dest


def convert_txt_to_singbox_json(input_path: Path, rule_type: str, output_json_path: Path) -> bool:
    """将文本规则列表转换为 sing-box JSON 格式"""
    if not input_path.exists():
        return False

    domain_suffixes = []
    ip_cidrs = []

    with open(input_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue

            if rule_type == "domain":
                # 去除通配符前缀，统一变成后缀匹配
                body = line
                if body.startswith("+."):
                    body = body[2:]
                elif body.startswith("*."):
                    body = body[2:]
                if body:
                    domain_suffixes.append(body.lower())
            else:
                # IP CIDR 规则
                # 兼容格式，防止多余的 IP-CIDR, 前缀
                if line.upper().startswith("IP-CIDR,"):
                    line = line.split(",")[1].strip()
                elif line.upper().startswith("IP-CIDR6,"):
                    line = line.split(",")[1].strip()
                ip_cidrs.append(line)

    # 构造符合 sing-box 1.x 规范的 rule-set 结构
    rules_block = {}
    if rule_type == "domain":
        domain_suffixes = sorted(set(domain_suffixes))
        rules_block["domain_suffix"] = domain_suffixes
    else:
        ip_cidrs = sorted(set(ip_cidrs))
        rules_block["ip_cidr"] = ip_cidrs

    payload = {
        "version": 1,
        "rules": [
            rules_block
        ]
    }

    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return True


def compile_ruleset(singbox_bin: Path, input_path: Path,
                    rule_type: str, output_name: str) -> bool:
    """转换并编译单个规则集"""
    if not input_path.exists():
        print(f"  [WARN] 输入文件不存在: {input_path}")
        return False

    temp_json = TEMP_DIR / f"{output_name}.json"
    output_srs = BUILD_DIR / f"{output_name}.srs"

    # 1. 转换文本为 json 规则集
    if not convert_txt_to_singbox_json(input_path, rule_type, temp_json):
        print(f"  [ERROR] JSON 转化失败 ({output_name})")
        return False

    # 2. 编译 json 为 srs 二进制
    cmd_srs = [
        str(singbox_bin),
        "rule-set",
        "compile",
        "--output",
        str(output_srs),
        str(temp_json)
    ]

    try:
        result = subprocess.run(cmd_srs, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  [ERROR] SRS 编译失败 ({output_name}):")
            print(f"    stderr: {result.stderr.strip()}")
            return False

        srs_size = output_srs.stat().st_size
        print(f"  ✅ {output_name}.srs: {srs_size / 1024:.1f} KB"
              f" (压缩比 {srs_size / input_path.stat().st_size * 100:.1f}%)")
        return True

    except subprocess.TimeoutExpired:
        print(f"  [ERROR] SRS 编译超时 ({output_name})")
        return False
    except Exception as e:
        print(f"  [ERROR] 编译异常 ({output_name}): {e}")
        return False


def main():
    print("=" * 60)
    print("ProxyRules — Sing-box 规则编译")
    print("=" * 60)

    # ---- 检查输入文件 ----
    print("\n[1/3] 检查输入文件...")
    missing = []
    for input_file, _type, _out in COMPILE_TASKS:
        fp = OUTPUT_DIR / input_file
        if fp.exists():
            print(f"  ✅ {input_file} ({fp.stat().st_size / 1024:.1f} KB)")
        else:
            print(f"  ❌ {input_file} — 缺失")
            missing.append(input_file)

    if missing:
        print(f"\n[ERROR] 缺少 {len(missing)} 个输入文件，请先运行 fetch_and_filter.py")
        return 1

    # ---- 获取 Sing-box 二进制 ----
    print("\n[2/3] 获取 Sing-box 核心...")
    singbox_bin = get_singbox_path()
    if singbox_bin is None:
        print("\n[ERROR] 无法获取 Sing-box 核心")
        return 1

    # 验证二进制可用
    try:
        result = subprocess.run(
            [str(singbox_bin), "version"],
            capture_output=True, text=True, timeout=10
        )
        if "sing-box version" in result.stdout or "sing-box version" in result.stderr or result.returncode == 0:
            print(f"  ✅ Sing-box 可用")
        else:
            print(f"  [ERROR] Sing-box 报错: {result.stderr[:200]}")
            return 1
    except Exception as e:
        print(f"  [ERROR] Sing-box 二进制不可用: {e}")
        return 1

    # ---- 编译所有规则集 ----
    print("\n[3/3] 编译规则集...")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    success_count = 0
    for input_file, rule_type, output_name in COMPILE_TASKS:
        input_path = OUTPUT_DIR / input_file
        if compile_ruleset(singbox_bin, input_path, rule_type, output_name):
            success_count += 1

    # 清理临时中间 json 文件夹
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception:
        pass

    # ---- 生成摘要 ----
    print("\n" + "=" * 60)
    print(f"编译完成: {success_count}/{len(COMPILE_TASKS)} 成功")

    if success_count > 0:
        print("\n📦 编译产物:")
        total_size = 0
        for f in sorted(BUILD_DIR.glob("*.srs")):
            size_kb = f.stat().st_size / 1024
            total_size += f.stat().st_size
            print(f"  {f.name}: {size_kb:.1f} KB")
        print(f"\n  总大小: {total_size / 1024:.1f} KB")

    return 0 if success_count == len(COMPILE_TASKS) else 1


if __name__ == "__main__":
    # 使脚本可以直接执行并加上 +x 属性
    sys.exit(main())
