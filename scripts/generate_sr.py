#!/usr/bin/env python3
"""
ProxyRules — Shadowrocket .sgmodule 模块生成脚本
=====================================================
从 output/*.txt 文本规则生成 Shadowrocket 订阅模块文件

生成产物:
  build/Shadowrocket/direct.sgmodule   — 直连域名 + IP
  build/Shadowrocket/reject.sgmodule   — 广告/追踪拦截
  build/Shadowrocket/private.sgmodule  — 私有 IP + 局域网域名

.sgmodule 格式:
  #!name = ModuleName
  #!desc = Description
  #!author = ProxyRules

  [Rule]
  DOMAIN-SUFFIX,example.com,DIRECT
  IP-CIDR,1.0.1.0/24,DIRECT
  DOMAIN-SUFFIX,ads.com,REJECT
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 北京时区
TZ_BEIJING = timezone(timedelta(hours=8))

BASE_DIR   = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
BUILD_DIR  = BASE_DIR / "build" / "Shadowrocket"

# 输入 → 输出映射
MODULE_DEFS = [
    {
        "name": "ProxyRules - Direct",
        "filename": "direct.sgmodule",
        "desc": "中国大陆域名、CDN与IP直连规则",
        "policy": "DIRECT",
        "inputs": [
            {"file": "direct_domain.txt", "type": "domain"},
            {"file": "direct_ip.txt",     "type": "ip"},
        ],
    },
    {
        "name": "ProxyRules - Reject",
        "filename": "reject.sgmodule",
        "desc": "广告、追踪、统计域名拦截规则",
        "policy": "REJECT",
        "inputs": [
            {"file": "reject.txt", "type": "domain"},
        ],
    },
]


def load_lines(filepath: Path) -> list:
    """读取规则文件，跳过注释头和空行，返回规则行列表"""
    if not filepath.exists():
        return []
    lines = []
    with open(filepath, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


def classify_and_format(entry: str, rule_type: str, policy: str) -> str:
    """
    将原始规则条目转换为 Shadowrocket 规则行

    域名规则:
      example.com  → DOMAIN-SUFFIX,example.com,POLICY
      *.example.com → DOMAIN-SUFFIX,example.com,POLICY  (strip *. prefix)
      +.example.com → DOMAIN-SUFFIX,example.com,POLICY  (strip +. prefix)

    IP 规则:
      1.0.1.0/24 → IP-CIDR,1.0.1.0/24,POLICY
    """
    if rule_type == "ip":
        # IP-CIDR 格式：确保前缀
        if not entry.upper().startswith("IP-CIDR"):
            return f"IP-CIDR,{entry},{policy}"
        else:
            # 已有前缀，替换策略
            return f"{entry.split(',')[0]},{entry.split(',')[1]},{policy}"

    # --- 域名规则 ---
    domain = entry

    # 去除通配符前缀，统一用 DOMAIN-SUFFIX
    if domain.startswith("*."):
        domain = domain[2:]
    elif domain.startswith("+."):
        domain = domain[2:]

    return f"DOMAIN-SUFFIX,{domain},{policy}"


def generate_module(mod_def: dict, timestamp: str) -> Path:
    """生成单个 .sgmodule 文件，返回写入的文件路径"""
    module_name = mod_def["name"]
    module_desc = mod_def["desc"]
    policy = mod_def["policy"]
    filename = mod_def["filename"]

    # 收集所有规则
    rules = []
    for inp in mod_def["inputs"]:
        filepath = OUTPUT_DIR / inp["file"]
        entries = load_lines(filepath)
        for entry in entries:
            rule_line = classify_and_format(entry, inp["type"], policy)
            rules.append(rule_line)

    # 去重 (Shadowrocket 模块中重复规则无意义)
    rules = sorted(set(rules))

    # 构建模块内容
    lines = []
    lines.append(f"#!name = {module_name}")
    lines.append(f"#!desc = {module_desc}")
    lines.append(f"#!author = ProxyRules")
    lines.append(f"#!homepage = https://github.com/ProxyRules/ProxyRules")
    lines.append(f"#!update = {timestamp}")
    lines.append(f"#!rule_count = {len(rules)}")
    lines.append("")
    lines.append("[Rule]")

    for rule in rules:
        lines.append(rule)

    # 写入文件
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BUILD_DIR / filename
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return out_path, len(rules)


def main():
    print("=" * 60)
    print("ProxyRules — Shadowrocket 模块生成")
    print("=" * 60)

    timestamp = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S (北京时间)")

    # 检查输入
    missing = [f.name for f in [OUTPUT_DIR / "direct_domain.txt",
                                 OUTPUT_DIR / "reject.txt"]
               if not f.exists()]
    if missing:
        print(f"[ERROR] 缺少输入文件: {missing}")
        print("  请先运行 fetch_and_filter.py")
        return 1

    total_rules = 0
    for mod_def in MODULE_DEFS:
        out_path, count = generate_module(mod_def, timestamp)
        total_rules += count
        print(f"  ✅ {mod_def['filename']}: {count:,} 条规则"
              f" ({out_path.stat().st_size / 1024:.1f} KB)")

    print(f"\n  总计: {total_rules:,} 条规则")
    print(f"  输出目录: {BUILD_DIR.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
