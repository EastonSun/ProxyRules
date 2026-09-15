#!/usr/bin/env python3
"""
ProxyRules — 全方位全自动失效规则清洗与大厂误杀自检脚本
===================================================
1. 解析 config/sources.yaml 中所有启用的上游源及其所有的规则文件链接。
2. 通过 HTTP HEAD 快速校验这些链接（使用多线程并发）。
3. 严格排除网络波动，仅在明确返回 404 时判定为失效。
4. 过滤并重新写回 sources.yaml，精准结合 (数据源名, 文件路径) 二元组定位并安全删除 404 条目，完美保留原有注释、缩进和格式。
5. 自动执行 fetch_and_filter.py、compile_singbox.py、compile_mihomo.py。
6. 排查 output/reject_domain.txt 中是否包含大厂主域的全局拦截（误杀检测），输出详细报告。
"""

import os
import re
import sys
import yaml
import requests
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 基础路径定义
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
OUTPUT_DIR = BASE_DIR / "output"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"

# 常见大厂主域白名单（用于检测 reject_domain.txt 中的全局拦截误杀）
BIG_COMPANY_DOMAINS = {
    "baidu.com", "alipay.com", "taobao.com", "jd.com", "apple.com",
    "google.com", "microsoft.com", "bilibili.com", "weixin.qq.com",
    "alicdn.com", "tencent.com", "qq.com", "163.com", "weibo.com",
    "sina.com.cn", "xiaomi.com", "huawei.com", "meituan.com",
    "douyin.com", "toutiao.com", "bytedance.com"
}


def load_sources_yaml() -> dict:
    """加载 sources.yaml"""
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_url(source_name: str, path: str, url: str) -> tuple:
    """
    对指定的 URL 进行 HTTP HEAD 校验。
    返回 (source_name, path, is_404, status_code_or_error_msg)
    """
    headers = {
        "User-Agent": "ProxyRules-Fetcher/1.0 (+https://github.com/EastonSun/ProxyRules)"
    }
    try:
        # 使用 HEAD 请求，超时 8 秒
        response = requests.head(url, headers=headers, timeout=8, allow_redirects=True)
        if response.status_code == 404:
            return source_name, path, True, 404
        else:
            return source_name, path, False, response.status_code
    except Exception as e:
        # 网络超时或连接异常等不作为 404 误杀判定
        return source_name, path, False, f"Error: {str(e)}"


def main():
    print("=" * 70)
    print("  ProxyRules 全功能上游失效链接自动清洗与大厂误杀自检启动")
    print("=" * 70)

    # 1. 解析 sources.yaml 提取所有启用源的规则文件
    if not SOURCES_PATH.exists():
        print(f"[错误] 找不到 sources.yaml 配置文件: {SOURCES_PATH}")
        sys.exit(1)

    try:
        sources_data = load_sources_yaml()
    except Exception as e:
        print(f"[错误] 解析 YAML 失败: {e}")
        sys.exit(1)

    check_tasks = []
    for src in sources_data.get("sources", []):
        if not src.get("enabled", True):
            continue
        
        src_name = src.get("name")
        base_url = src.get("base_url", "").rstrip("/")
        files_list = src.get("files", [])

        if not src_name or not base_url:
            continue

        for file_entry in files_list:
            path_val = file_entry.get("path")
            if path_val:
                full_url = f"{base_url}/{path_val}"
                check_tasks.append((src_name, path_val, full_url))

    print(f"[信息] 检索到当前所有启用数据源中，共 {len(check_tasks)} 个上游文件链接需要校验。")

    # 2. 并发 HTTP HEAD 校验
    print("[运行] 正在并发校验所有链接状态（采用 30 线程），请稍候...")
    invalid_keys = set()  # 存放 (source_name, path) 元组
    errors_count = 0
    success_count = 0

    with ThreadPoolExecutor(max_workers=30) as executor:
        future_to_task = {
            executor.submit(check_url, sname, pval, url): (sname, pval) 
            for sname, pval, url in check_tasks
        }
        for future in as_completed(future_to_task):
            sname, pval = future_to_task[future]
            try:
                sname_res, pval_res, is_404, status_info = future.result()
                if is_404:
                    print(f"  [发现 404 失效] 数据源: {sname_res} | 路径: {pval_res}")
                    invalid_keys.add((sname_res, pval_res))
                elif isinstance(status_info, str) and status_info.startswith("Error"):
                    errors_count += 1
                else:
                    success_count += 1
            except Exception as exc:
                print(f"[警告] 校验任务执行异常: {exc}")
                errors_count += 1

    print(f"\n[结果] 校验完成。共发现 {len(invalid_keys)} 个 404 失效规则链接，{success_count} 个正常连接，{errors_count} 个偶发性网络错误/超时。")

    # 3. 彻底从 sources.yaml 中自动删除 404 失效条目并保留原有格式
    if invalid_keys:
        print(f"[运行] 正在精准清除 sources.yaml 中的这 {len(invalid_keys)} 个失效条目并重新写回文件...")
        with open(SOURCES_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        skip_count = 0
        removed_count = 0
        current_source = None

        for i, line in enumerate(lines):
            if skip_count > 0:
                skip_count -= 1
                continue

            # 检测当前所处数据源的名称
            source_match = re.search(r'-\s+name:\s*([^\s#]+)', line)
            if source_match:
                current_source = source_match.group(1).strip()

            # 检测 path
            path_match = re.search(r'-\s+path:\s*([^\s#]+)', line)
            if path_match:
                path_val = path_match.group(1).strip()
                # 如果当前 (数据源名称, path_val) 在失效集合中
                if current_source and (current_source, path_val) in invalid_keys:
                    to_skip = 0
                    # 寻找该条目紧随其后的 format / category 行，并一同跳过/删除
                    for offset in range(1, 4):
                        if i + offset < len(lines):
                            next_line = lines[i + offset]
                            if re.search(r'^\s+(format|category):', next_line):
                                to_skip = offset
                    skip_count = to_skip
                    removed_count += 1
                    print(f"  -> 已成功滤除 sources.yaml 中数据源 [{current_source}] 下的 [{path_val}] 规则条目")
                    continue

            new_lines.append(line)

        # 写入文件
        with open(SOURCES_PATH, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        print(f"[成功] sources.yaml 深度清洗完成！共安全移除了 {removed_count} 组失效规则配置。")
    else:
        print("[提示] 没有任何 404 失效规则，不需要对 sources.yaml 进行写入操作。")

    # 4. 运行 scripts/fetch_and_filter.py 重新生成全新的过滤规则
    print("\n" + "=" * 70)
    print("  [步骤] 运行 fetch_and_filter.py 生成全新过滤规则")
    print("=" * 70)
    
    fetch_script = BASE_DIR / "scripts" / "fetch_and_filter.py"
    try:
        subprocess.run([sys.executable, str(fetch_script)], check=True)
        print("[成功] 规则提取与过滤成功运行完毕。")
    except subprocess.CalledProcessError as e:
        print(f"[错误] 运行 fetch_and_filter.py 失败: {e}")
        sys.exit(1)

    # 运行 compile_singbox.py 等编译脚本
    print("\n" + "=" * 70)
    print("  [步骤] 运行编译脚本（compile_singbox.py & compile_mihomo.py）")
    print("=" * 70)
    
    compile_sb = BASE_DIR / "scripts" / "compile_singbox.py"
    compile_mh = BASE_DIR / "scripts" / "compile_mihomo.py"
    
    try:
        print("[运行] 编译 Sing-box 规则集...")
        subprocess.run([sys.executable, str(compile_sb)], check=True)
        print("[成功] Sing-box 编译完毕。")
    except subprocess.CalledProcessError as e:
        print(f"[警告] 编译 Sing-box 失败: {e}")

    try:
        print("[运行] 编译 Mihomo 规则集...")
        subprocess.run([sys.executable, str(compile_mh)], check=True)
        print("[成功] Mihomo 编译完毕。")
    except subprocess.CalledProcessError as e:
        print(f"[警告] 编译 Mihomo 失败: {e}")

    # 5. 自检 output/reject_domain.txt 中的大厂主域全局拦截误杀
    print("\n" + "=" * 70)
    print("  [步骤] 大厂白名单误杀自检报表 (reject_domain.txt)")
    print("=" * 70)

    reject_txt_path = OUTPUT_DIR / "reject_domain.txt"
    if not reject_txt_path.exists():
        print(f"[错误] 未能找到生成的拦截库文件: {reject_txt_path}")
        sys.exit(1)

    triggered_warnings = []
    
    # 逐行读取排查
    with open(reject_txt_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            
            # 清理出域名
            clean_dom = line_str
            if clean_dom.startswith("+."):
                clean_dom = clean_dom[2:]
            elif clean_dom.startswith("."):
                clean_dom = clean_dom[1:]
            
            if clean_dom in BIG_COMPANY_DOMAINS:
                triggered_warnings.append((idx, line_str, clean_dom))

    if triggered_warnings:
        print("⚠️ ⚠️ ⚠️  [报警] 检测到广告拦截库中存在对大厂主域根域的全局误杀拦截！ ⚠️ ⚠️ ⚠️")
        print(f"共发现 {len(triggered_warnings)} 处可疑拦截条目：")
        print("-" * 70)
        print(f"{'行号':<8} | {'拦截规则':<30} | {'被拦截主域'}")
        print("-" * 70)
        for row, rule, domain in triggered_warnings:
            print(f"{row:<8} | {rule:<30} | {domain}")
        print("-" * 70)
        print("[建议] 请立即检查这些上游拦截规则，或在 config/remove_reject_domain.txt 中添加强制白名单以排除这些误杀！")
    else:
        print("✅ [通过] 未在 reject_domain.txt 中发现任何大厂主域根域的全局误杀条目，质量状态极佳！")

    print("\n" + "=" * 70)
    print("  全自动清洗与校验诊断任务顺利结束！")
    print("=" * 70)


if __name__ == "__main__":
    main()
