#!/usr/bin/env python3
"""
ProxyRules — Mihomo 规则编译脚本
=====================================
下载 Mihomo 核心，将 output/*.txt 文本规则编译为:
  1. 编译后文本格式 (text) — 标准 Mihomo rule-set 文本格式
  2. MRS 二进制格式 (.mrs) — Mihomo 专有高效格式

编译映射:
  direct_domain.txt  → direct_domain.mrs  (domain)
  direct_ip.txt      → direct_ip.mrs      (ipcidr)
  private_ip.txt     → private_ip.mrs     (ipcidr)
  private_domain.txt → private_domain.mrs (domain)
  reject.txt         → reject.mrs         (domain)

Mihomo 命令:
  mihomo convert-ruleset domain  text input.txt output.mrs
  mihomo convert-ruleset ipcidr  text input.txt output.mrs
"""

import os
import sys
import subprocess
import platform
import stat
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

TZ_BEIJING = timezone(timedelta(hours=8))

BASE_DIR   = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
BUILD_DIR  = BASE_DIR / "build"
CACHE_DIR  = BASE_DIR / ".cache"

# Mihomo release URL (latest stable)
MIHOMO_REPO = "https://github.com/MetaCubeX/mihomo/releases"
# Prefer alpha for latest convert-ruleset support
MIHOMO_VERSION = "v1.19.3"

# 架构 → mihomo release asset 后缀
ARCH_MAP = {
    ("linux", "x86_64"):  "mihomo-linux-amd64-{version}.gz",
    ("linux", "aarch64"): "mihomo-linux-arm64-{version}.gz",
    ("darwin", "x86_64"): "mihomo-darwin-amd64-{version}.gz",
    ("darwin", "arm64"):  "mihomo-darwin-arm64-{version}.gz",
}

# 编译任务定义: (输入文件名, 规则类型, 输出文件名)
COMPILE_TASKS = [
    ("direct_domain.txt",  "domain", "direct_domain"),
    ("direct_ip.txt",      "ipcidr", "direct_ip"),
    ("private_ip.txt",     "ipcidr", "private_ip"),
    ("private_domain.txt", "domain", "private_domain"),
    ("reject.txt",         "domain", "reject"),
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


def get_mihomo_path() -> Optional[Path]:
    """
    获取 Mihomo 二进制路径
    1. 检查系统 PATH 中的 mihomo
    2. 检查缓存目录
    3. 下载到缓存目录
    """
    # 1. PATH 中查找
    result = subprocess.run(["which", "mihomo"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        path = Path(result.stdout.strip())
        if path.exists():
            print(f"  使用系统 mihomo: {path}")
            return path

    # 2. 缓存目录
    sys_name, arch = detect_platform()
    cache_key = f"{sys_name}-{arch}-{MIHOMO_VERSION}"
    cached_bin = CACHE_DIR / f"mihomo-{cache_key}"

    if cached_bin.exists():
        print(f"  使用缓存 mihomo: {cached_bin}")
        return cached_bin

    # 3. 下载
    return download_mihomo(sys_name, arch, cached_bin)


def download_mihomo(sys_name: str, arch: str, dest: Path) -> Optional[Path]:
    """下载 Mihomo 二进制到缓存目录"""
    key = (sys_name, arch)
    if key not in ARCH_MAP:
        print(f"  [ERROR] 不支持的平台: {sys_name}/{arch}")
        print(f"  支持的平台: {list(ARCH_MAP.keys())}")
        return None

    asset_pattern = ARCH_MAP[key].format(version=MIHOMO_VERSION)
    download_url = f"{MIHOMO_REPO}/download/{MIHOMO_VERSION}/{asset_pattern}"

    print(f"  下载 mihomo {MIHOMO_VERSION}: {download_url}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 下载 .gz 文件
    gz_path = CACHE_DIR / asset_pattern
    try:
        resp = requests.get(download_url, stream=True, timeout=120)
        resp.raise_for_status()

        with open(gz_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        print(f"  下载完成: {gz_path.stat().st_size / 1024 / 1024:.1f} MB")
    except Exception as e:
        print(f"  [ERROR] 下载失败: {e}")
        # 嘗試 fallback 到 latest release
        return download_mihomo_latest(sys_name, arch, dest)

    # 解压
    import gzip
    dest.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz_path, "rb") as gz_f:
        with open(dest, "wb") as out_f:
            out_f.write(gz_f.read())

    # 设置可执行权限
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # 清理 gz 文件
    gz_path.unlink(missing_ok=True)

    print(f"  解压完成: {dest}")
    return dest


def download_mihomo_latest(sys_name: str, arch: str, dest: Path) -> Optional[Path]:
    """Fallback: 获取最新 release 版本号并下载"""
    print("  尝试获取最新版本...")
    try:
        api_url = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
        resp = requests.get(api_url, timeout=30)
        resp.raise_for_status()
        latest_tag = resp.json()["tag_name"]
        print(f"  最新版本: {latest_tag}")

        global MIHOMO_VERSION
        MIHOMO_VERSION = latest_tag

        return download_mihomo(sys_name, arch, dest)
    except Exception as e:
        print(f"  [ERROR] 获取最新版本失败: {e}")
        return None


def compile_ruleset(mihomo_bin: Path, input_path: Path,
                    rule_type: str, output_name: str) -> bool:
    """
    编译单个规则集

    Args:
        mihomo_bin: mihomo 二进制路径
        input_path: 输入 .txt 文件
        rule_type: 'domain' 或 'ipcidr'
        output_name: 输出文件名前缀 (不含扩展名)

    Returns:
        成功返回 True
    """
    if not input_path.exists():
        print(f"  [WARN] 输入文件不存在: {input_path}")
        return False

    output_mrs = BUILD_DIR / f"{output_name}.mrs"

    # 1. 编译为 MRS 二进制
    cmd_mrs = [
        str(mihomo_bin),
        "convert-ruleset",
        rule_type,
        "text",
        str(input_path),
        str(output_mrs),
    ]

    try:
        result = subprocess.run(cmd_mrs, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  [ERROR] MRS 编译失败 ({output_name}):")
            print(f"    stderr: {result.stderr.strip()}")
            return False

        mrs_size = output_mrs.stat().st_size
        print(f"  ✅ {output_name}.mrs: {mrs_size / 1024:.1f} KB"
              f" (压缩比 {mrs_size / input_path.stat().st_size * 100:.1f}%)")
        return True

    except subprocess.TimeoutExpired:
        print(f"  [ERROR] MRS 编译超时 ({output_name})")
        return False
    except Exception as e:
        print(f"  [ERROR] 编译异常 ({output_name}): {e}")
        return False


def main():
    print("=" * 60)
    print("ProxyRules — Mihomo 规则编译")
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

    # ---- 获取 Mihomo 二进制 ----
    print("\n[2/3] 获取 Mihomo 核心...")
    mihomo_bin = get_mihomo_path()
    if mihomo_bin is None:
        print("\n[ERROR] 无法获取 Mihomo 核心")
        print("  手动安装: 从 https://github.com/MetaCubeX/mihomo/releases 下载")
        print("  并放到 PATH 中或项目根目录")
        return 1

    # 验证二进制可用
    try:
        # 直接用 convert-ruleset 的报错来判断是否可用
        # mihomo 没有 version 子命令，直接测试 convert-ruleset
        result = subprocess.run(
            [str(mihomo_bin), "convert-ruleset"],
            capture_output=True, text=True, timeout=10
        )
        # 预期输出 usage 信息（panic 退出码≠0，但 stderr 中有用法说明）
        if "Usage: convert-ruleset" in result.stderr or "Usage: convert-ruleset" in result.stdout:
            print(f"  ✅ Mihomo convert-ruleset 可用")
        else:
            print(f"  [ERROR] Mihomo 不支持 convert-ruleset: {result.stderr[:200]}")
            return 1
    except Exception as e:
        print(f"  [ERROR] Mihomo 二进制不可用: {e}")
        return 1

    # ---- 编译所有规则集 ----
    print("\n[3/3] 编译规则集...")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    success_count = 0
    for input_file, rule_type, output_name in COMPILE_TASKS:
        input_path = OUTPUT_DIR / input_file
        if compile_ruleset(mihomo_bin, input_path, rule_type, output_name):
            success_count += 1

    # ---- 生成摘要 ----
    print("\n" + "=" * 60)
    print(f"编译完成: {success_count}/{len(COMPILE_TASKS)} 成功")

    if success_count > 0:
        print("\n📦 编译产物:")
        total_size = 0
        for f in sorted(BUILD_DIR.glob("*.mrs")):
            size_kb = f.stat().st_size / 1024
            total_size += f.stat().st_size
            print(f"  {f.name}: {size_kb:.1f} KB")
        print(f"\n  总大小: {total_size / 1024:.1f} KB")

    return 0 if success_count == len(COMPILE_TASKS) else 1


if __name__ == "__main__":
    sys.exit(main())
