#!/usr/bin/env python3
"""
ProxyRules — 上游规则抓取与清洗脚本
=======================================
1. 从 12 个上游源抓取原始规则文件
2. 按 8 种格式解析出纯域名 / IP-CIDR
3. 按 5 个分类分桶 + 关键词过滤
4. 应用手动干预文件 (add_*/remove_*)
5. 全局排除 + 去重 + 排序 + 写入 output/
"""

import os
import re
import sys
import time
import gzip
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import OrderedDict
from typing import Optional

import yaml
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# 路径常量
# ============================================================
BASE_DIR    = Path(__file__).resolve().parent.parent
CONFIG_DIR  = BASE_DIR / "config"
OUTPUT_DIR  = BASE_DIR / "output"
CACHE_DIR   = BASE_DIR / ".cache"

SOURCES_FILE    = CONFIG_DIR / "sources.yaml"
FILTERS_FILE    = CONFIG_DIR / "filters.yaml"
ADD_DIRECT      = CONFIG_DIR / "add_direct.txt"
REMOVE_DIRECT   = CONFIG_DIR / "remove_direct.txt"
ADD_REJECT      = CONFIG_DIR / "add_reject.txt"
REMOVE_REJECT   = CONFIG_DIR / "remove_reject.txt"

CATEGORIES = ["direct_domain", "direct_ip", "private_ip", "private_domain", "reject"]

# 北京时区
TZ_BEIJING = timezone(timedelta(hours=8))


# ============================================================
# 配置加载
# ============================================================

def load_yaml(path: Path) -> dict:
    """加载 YAML 配置文件"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_txt_lines(path: Path) -> list:
    """
    加载 TXT 手动干预文件
    返回: 非空、非注释行的域名列表（已 strip、已小写）
    """
    if not path.exists():
        return []
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            lines.append(line.lower())
    return lines


# ============================================================
# HTTP 客户端
# ============================================================

def build_session(global_cfg: dict) -> requests.Session:
    """构建带重试的 HTTP Session"""
    session = requests.Session()
    retries = Retry(
        total=global_cfg.get("max_retries", 3),
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": global_cfg.get("user_agent", "ProxyRules-Fetcher/1.0"),
        "Accept": "text/plain,application/octet-stream,*/*",
    })
    return session


def fetch_url(session: requests.Session, url: str, timeout: int) -> Optional[str]:
    """
    抓取单个 URL，返回文本内容
    失败返回 None
    """
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        # 自动检测 gzip 压缩的 YAML 文件 (如 Cats-Team 的 ad.yaml)
        content = resp.content
        if url.endswith(".gz") or (len(content) >= 2 and content[:2] == b'\x1f\x8b'):
            content = gzip.decompress(content)
        return content.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] 抓取失败 {url}: {e}")
        return None


# ============================================================
# 格式解析器
# ============================================================

class FormatParser:
    """将各种上游格式统一解析为纯域名或 IP-CIDR"""

    @staticmethod
    def _is_comment(line: str, comment_chars: list) -> bool:
        """判断是否为注释行"""
        stripped = line.strip()
        if not stripped:
            return True
        for ch in comment_chars:
            if stripped.startswith(ch):
                return True
        return False

    @staticmethod
    def _extract_domain_from_line(line: str, prefixes: list) -> Optional[str]:
        """
        从 Clash/Surge 前缀行中提取纯域名
        如 'DOMAIN-SUFFIX,example.com' → 'example.com'
        """
        stripped = line.strip()
        for prefix in prefixes:
            if stripped.startswith(prefix):
                domain = stripped[len(prefix):].strip()
                # 去除可能的后缀参数
                # 如 ',DIRECT' ',PROXY' ',REJECT' 'no-resolve'
                for suffix in [",DIRECT", ",PROXY", ",REJECT", ",REJECT-TLD",
                               ",no-resolve", "no-resolve"]:
                    if domain.endswith(suffix):
                        domain = domain[:-len(suffix)].strip()
                # 如果逗号还在（如 surge 格式），取第一段之前
                if "," in domain:
                    domain = domain.split(",")[0].strip()
                if domain:
                    return domain.lower()
        return None

    @classmethod
    def parse_domain_list(cls, text: str, prefixes: list) -> set:
        """解析 domain-list 格式 (行首带 DOMAIN-SUFFIX, / DOMAIN, 等前缀)"""
        result = set()
        for line in text.splitlines():
            if cls._is_comment(line, ["#", "//", "!", ";"]):
                continue
            domain = cls._extract_domain_from_line(line, prefixes)
            if domain:
                result.add(domain)
        return result

    @classmethod
    def parse_bare_domain(cls, text: str) -> set:
        """
        解析纯域名格式 — 一行一个域名
        支持以下变体 (MetaCubeX .list 等):
          +.example.com    — Surge 风格域名后缀
          example.com      — 纯域名
        """
        import re as _re
        result = set()
        # 合法的域名: 可选 +. 前缀，然后是域名
        domain_re = _re.compile(r'^(\+\.)?[a-zA-Z0-9][a-zA-Z0-9._*-]*[a-zA-Z0-9]$')
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("!"):
                continue
            # 排除 IP 地址
            if _re.match(r'^\d+\.\d+\.\d+\.\d+$', stripped):
                continue
            # 排除 IP-CIDR 格式
            if stripped.startswith("IP-CIDR"):
                continue
            # 去掉 +. 前缀 (Surge 风格)
            if stripped.startswith("+."):
                stripped = stripped[2:]
            # 验证是否为有效域名格式
            if domain_re.match(stripped) and "." in stripped:
                result.add(stripped.lower())
        return result

    @classmethod
    def parse_ip_cidr(cls, text: str) -> set:
        """解析 IP-CIDR 格式"""
        result = set()
        regex = re.compile(
            r'(?:IP-CIDR6?,)?'
            r'('
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?'  # IPv4
            r'|'
            r'[0-9a-fA-F:]+(?:/\d{1,3})?'  # IPv6
            r')'
        )
        for line in text.splitlines():
            if cls._is_comment(line, ["#", "//", "!", ";"]):
                continue
            m = regex.search(line)
            if m:
                cidr = m.group(1).strip()
                if cidr and "/" in cidr:  # 必须是 CIDR 格式
                    result.add(cidr)
        return result

    @classmethod
    def parse_clash_rule_set(cls, text: str) -> set:
        """
        解析 Clash YAML rule-provider 格式
        格式:
          payload:
            - 'DOMAIN-SUFFIX,example.com'
            - 'example.com'
            - '+.example.com'
        """
        result = set()
        prefixes = ["DOMAIN-SUFFIX,", "DOMAIN,", "DOMAIN-KEYWORD,",
                     "IP-CIDR,", "IP-CIDR6,", "PROCESS-NAME,",
                     "+.", ".", "GEOSITE,", "GEOIP,"]
        in_payload = False
        for line in text.splitlines():
            stripped = line.strip()

            # 注释行
            if stripped.startswith("#"):
                continue

            # 检测 payload 段开始
            if stripped == "payload:":
                in_payload = True
                continue

            if not in_payload:
                continue

            # payload 内容: 以 '- ' 或 '-' 开头
            if not stripped.startswith("-"):
                # payload 结束 — 遇到非缩进、非列表项的顶层 key
                if stripped and not stripped.startswith(" "):
                    in_payload = False
                continue

            # 提取: - 'xxx' 或 - "xxx" 或 - xxx
            item = stripped[1:].strip()  # 去掉 '-'

            # 去掉外层引号 (单引号或双引号)
            if len(item) >= 2:
                if (item.startswith("'") and item.endswith("'")) or \
                   (item.startswith('"') and item.endswith('"')):
                    item = item[1:-1].strip()

            # 提取域名
            domain = cls._extract_domain_from_line(item, prefixes)
            if domain:
                result.add(domain)

        return result

    @classmethod
    def parse_surge_list(cls, text: str) -> set:
        """
        解析 Surge 风格规则格式:
          DOMAIN-SUFFIX,example.com,DIRECT
          +.example.com
          .example.com
          IP-CIDR,1.0.1.0/24,no-resolve
        """
        result = set()
        prefixes = ["DOMAIN-SUFFIX,", "DOMAIN,", "DOMAIN-KEYWORD,",
                     "IP-CIDR,", "IP-CIDR6,", "HOST-SUFFIX,", "HOST-KEYWORD,"]
        for line in text.splitlines():
            if cls._is_comment(line, ["#", "//", "!", ";"]):
                continue
            stripped = line.strip()

            # 处理 +.domain 和 .domain 格式 (xkww3n)
            if stripped.startswith("+."):
                domain = stripped[2:].strip()
                # 去掉可能的策略后缀
                if "," in domain:
                    domain = domain.split(",")[0].strip()
                if domain and "." in domain:
                    result.add(domain.lower())
                continue
            if stripped.startswith(".") and not stripped.startswith(".", 1):
                # .domain — 但排除 .. 开头的非法格式
                domain_part = stripped[1:]
                if "," in domain_part:
                    domain_part = domain_part.split(",")[0].strip()
                if domain_part and "." in domain_part:
                    result.add(domain_part.lower())
                continue

            # 处理带前缀的格式
            domain = cls._extract_domain_from_line(stripped, prefixes)
            if domain:
                result.add(domain)
        return result

    @classmethod
    def parse_adguard_list(cls, text: str) -> set:
        """解析 AdGuard 格式 (||domain^)"""
        result = set()
        # 去除修饰符
        modifiers = ["$important", "$badfilter", "$all", "$document",
                     "$popup", "$third-party", "$script", "$image",
                     "$stylesheet", "$object", "$xmlhttprequest",
                     "$subdocument", "$ping", "$websocket", "$webrtc"]
        for line in text.splitlines():
            if cls._is_comment(line, ["#", "!"]):
                continue
            stripped = line.strip()
            # 格式: ||example.com^
            # 格式: 0.0.0.0 example.com
            # 格式: 127.0.0.1 example.com
            # 格式: example.com
            # 去除开头的 ||
            if stripped.startswith("||"):
                stripped = stripped[2:]
            # 去除 ^ 及之后
            if "^" in stripped:
                stripped = stripped.split("^")[0]
            # 去除 IP 前缀
            if stripped.startswith("0.0.0.0 "):
                stripped = stripped[8:]
            elif stripped.startswith("127.0.0.1 "):
                stripped = stripped[10:]
            # 去除修饰符
            for mod in modifiers:
                if mod in stripped:
                    stripped = stripped.split(mod)[0]
            # 验证是否为有效域名
            stripped = stripped.strip()
            if "." in stripped and not stripped.startswith("/"):
                result.add(stripped.lower())
        return result

    @classmethod
    def parse_hosts_list(cls, text: str) -> set:
        """解析 hosts 文件格式 (0.0.0.0 domain)"""
        result = set()
        for line in text.splitlines():
            if cls._is_comment(line, ["#"]):
                continue
            parts = line.strip().split()
            # hosts 格式: IP domain [domain2 ...]
            if len(parts) >= 2:
                ip = parts[0]
                # 验证第一部分是否为 IP
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip) or re.match(r'^[0-9a-fA-F:]+$', ip):
                    for domain in parts[1:]:
                        domain = domain.strip().lower()
                        if "." in domain:
                            result.add(domain)
        return result

    @classmethod
    def parse_dnsmasq_conf(cls, text: str) -> set:
        """解析 dnsmasq 格式 (server=/domain/114.114.114.114)"""
        result = set()
        regex = re.compile(r'server=/([^/]+)/')
        for line in text.splitlines():
            if cls._is_comment(line, ["#"]):
                continue
            m = regex.search(line)
            if m:
                domain = m.group(1).strip().lower()
                if domain and "." in domain:
                    result.add(domain)
        return result


# ============================================================
# 分类逻辑
# ============================================================

def is_private_ip(cidr: str) -> bool:
    """判断一个 IP-CIDR 是否为私有/保留地址"""
    import ipaddress
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        return net.is_private or net.is_loopback or net.is_link_local or net.is_multicast or net.is_reserved
    except ValueError:
        return False


def match_keywords(domain: str, keywords: list) -> bool:
    """域名是否包含任一关键词"""
    lower = domain.lower()
    return any(kw.lower() in lower for kw in keywords)


def match_pattern(domain: str, patterns: list) -> bool:
    """域名是否匹配任一通配符模式"""
    lower = domain.lower()
    for pat in patterns:
        pat = pat.lower().strip()
        if pat.startswith("*."):
            suffix = pat[2:]
            if lower == suffix or lower.endswith("." + suffix):
                return True
        elif pat.startswith("."):
            suffix = pat[1:]
            if lower.endswith("." + suffix):
                return True
        elif pat == lower:
            return True
    return False


def match_any_pattern(domain: str, patterns: list) -> bool:
    """域名匹配任一通配符模式（与 match_pattern 相同，语义别名）"""
    return match_pattern(domain, patterns)


def clean_domain_set(items: set) -> set:
    """
    清理域名集合中的无效条目：
    - 排除 */ 开头（上游标记为"仅路径"的条目）
    - 排除 - 开头（部分上游的特殊标记，如 -tracker.xxx）
    - 排除纯 IP/CIDR
    - 排除含非法字符的行
    - 排除无有效 TLD 的条目（单标签、纯数字+IDN后缀等解析残片）
    - 输出统一使用 Mihomo +. 通配符格式
    """
    import re as _re
    cleaned = set()
    ip_re = _re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?$')
    # 提取通配符域名的 body 部分
    wildcard_body_re = _re.compile(r'^(?:\*|\+)\.(.+)$')
    for item in items:
        item = item.strip()
        if not item:
            continue
        # 去掉 *. 或 +. 前缀，统一转为裸域名后再加 +. 前缀
        body = item
        is_wildcard = False
        if item.startswith('*.') or item.startswith('+.'):
            wm = wildcard_body_re.match(item)
            if wm:
                body = wm.group(1)
                if not body:
                    continue
                is_wildcard = True
            else:
                body = item
        # 排除以 . 开头 (如 .engage.3m.) 或单点开头的残缺条目
        if body.startswith('.'):
            continue
        # 排除以 . 结尾的残缺条目 (如 analytics-cdn.)
        if body.endswith('.'):
            continue
        # 排除 */ — 标记为仅路径匹配
        if body.startswith('*/'):
            continue
        # 排除 - 开头（如 -tracker.xxx）
        if body.startswith('-'):
            continue
        # 排除纯 IP/CIDR
        if ip_re.match(body):
            continue
        # 排除明显不是域名的内容（含空格/括号等）
        if any(c in body for c in (' ', ',', '(', ')', '{', '}', '[', ']', '<', '>', ';', '"', "'")):
            continue
        # --- 域名结构校验 ---
        # 1. 非通配符条目须至少有一个点号（至少两段标签）
        #    通配符条目（如 *.local → +.local）允许单标签 body
        if not is_wildcard and '.' not in body:
            continue
        # 2. 对于 TLD 为 IDN（xn--）的条目，第二段标签不能是纯数字
        #    如：0.xn--czrs0t (0.商店)、001.xn--vhquv (001.企业) 均为上游解析残片
        parts = body.split('.')
        tld = parts[-1]
        if tld.startswith('xn--'):
            second = parts[-2] if len(parts) >= 2 else ''
            # 纯数字标签不属于合法注册域名
            if second.isdigit():
                continue
        # 统一输出 +. 前缀格式 (Mihomo DOMAIN-SUFFIX 等价)
        cleaned.add('+.' + body.lower())
    return cleaned


def filter_domain_set(
    raw_domains: set,
    cat_cfg: dict,
    global_exclude: dict,
    manual_add: list,
    manual_remove: list,
) -> set:
    """
    对一个原始域名集合执行完整的过滤流水线:
    1. 应用 category 的 include_keywords (保留匹配的)
    2. 应用 category 的 exclude_patterns (删除匹配的)
    3. 应用 global_exclude 的 domains / TLDs
    4. 应用 manual_remove
    5. 添加 manual_add
    """
    result = raw_domains.copy()

    # --- 1. 关键词保留 (如果配置了 include_keywords，则只保留包含关键词的域名) ---
    # 注意：direct_domain 不应配置 include_keywords，否则会把 99% 的大陆域名过滤掉
    # 此功能仅用于从 proxy-list 等泛文件中提取特定条目（如 apple-cn 等 CDN 域名）
    include_kw = cat_cfg.get("include_keywords", [])
    if include_kw:
        result = {d for d in result if match_keywords(d, include_kw)}

    # --- 2. 排除模式 ---
    exclude_pats = cat_cfg.get("exclude_patterns", [])
    if exclude_pats:
        result = {d for d in result if not match_pattern(d, exclude_pats)}

    # --- 3. 全局排除 ---
    # 具体域名（含子域名后缀匹配：排除 example.com 同时排除 sub.example.com）
    for excl_domain in global_exclude.get("domains", []):
        excl_lower = excl_domain.lower()
        result = {d for d in result if d != excl_lower and not d.endswith("." + excl_lower)}
    # 顶级域名
    for tld in global_exclude.get("exclude_tlds", []):
        tld_lower = tld.lower()
        result = {d for d in result if not d.endswith(tld_lower)}

    # --- 4. 手动删除 ---
    # manual_remove 支持精确匹配和通配符
    for rm in manual_remove:
        if rm.startswith("*.") or rm.startswith("."):
            result = {d for d in result if not match_pattern(d, [rm])}
        else:
            result.discard(rm)

    # --- 5. 手动添加 ---
    for add in manual_add:
        result.add(add)

    return result


def filter_ip_set(
    raw_cidrs: set,
    exclude_private: bool,
    manual_add: list,
    manual_remove: list,
) -> set:
    """对 IP-CIDR 集合执行过滤"""
    result = raw_cidrs.copy()

    # 私有地址剥离
    if exclude_private:
        result = {c for c in result if not is_private_ip(c)}

    for rm in manual_remove:
        result.discard(rm)
    for add in manual_add:
        result.add(add)

    return result


# ============================================================
# CIDR 去冗余
# ============================================================

def compact_cidr_set(cidrs: set) -> set:
    """
    对 CIDR 集合做压缩：如果一组更具体的 CIDR 完全覆盖一个超网，
    则移除超网保留具体 CIDR。
    例如：224.0.0.0/3 被 224.0.0.0/4 + 240.0.0.0/4 完全覆盖，移除 /3。
    """
    import ipaddress

    result = set(cidrs)

    # 对于 private_ip 的特殊处理：移除已知冗余超网
    # 224.0.0.0/3 == 224.0.0.0/4 (组播) + 240.0.0.0/4 (保留/E类)
    if "224.0.0.0/3" in result and "224.0.0.0/4" in result and "240.0.0.0/4" in result:
        result.discard("224.0.0.0/3")

    return result


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 60)
    print("ProxyRules — 规则抓取与清洗")
    print(f"启动时间: {datetime.now(TZ_BEIJING).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 60)

    # ---- 加载配置 ----
    print("\n[1/6] 加载配置文件...")
    sources_cfg = load_yaml(SOURCES_FILE)
    filters_cfg = load_yaml(FILTERS_FILE)
    global_cfg   = sources_cfg.get("global", {})
    cat_configs  = filters_cfg.get("categories", {})
    global_excl  = filters_cfg.get("global_exclude", {})
    format_cfgs  = filters_cfg.get("format_parsers", {})
    post_cfg     = filters_cfg.get("post_processing", {})

    # 加载手动干预文件
    manual = {
        "direct_domain": {
            "add": load_txt_lines(ADD_DIRECT),
            "remove": load_txt_lines(REMOVE_DIRECT),
        },
        "direct_ip": {
            "add": [],
            "remove": [],
        },
        "private_ip": {
            "add": [],
            "remove": [],
        },
        "private_domain": {
            "add": [],
            "remove": [],
        },
        "reject": {
            "add": load_txt_lines(ADD_REJECT),
            "remove": load_txt_lines(REMOVE_REJECT),
        },
    }
    print(f"  配置加载完成: {len(sources_cfg.get('sources', []))} 个上游源")

    # ---- 构建 HTTP 客户端 ----
    session = build_session(global_cfg)
    fetch_timeout = global_cfg.get("fetch_timeout", 30)

    # ---- 收集所有待抓取 URL ----
    print("\n[2/6] 收集上游文件列表...")
    fetch_tasks = []
    enabled_count = 0
    for src in sources_cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        enabled_count += 1
        base = src["base_url"]
        for f in src["files"]:
            url = f"{base.rstrip('/')}/{f['path'].lstrip('/')}"
            fetch_tasks.append({
                "url": url,
                "source": src["name"],
                "format": f["format"],
                "category": f["category"],
            })
    print(f"  可用源: {enabled_count}, 待抓取文件: {len(fetch_tasks)}")

    # ---- 抓取所有上游 ----
    print("\n[3/6] 抓取上游规则...")
    raw_buckets = {cat: set() for cat in CATEGORIES}
    success_count = 0
    fail_count = 0

    for i, task in enumerate(fetch_tasks, 1):
        url      = task["url"]
        fmt      = task["format"]
        cat      = task["category"]
        src_name = task["source"]

        print(f"  [{i}/{len(fetch_tasks)}] {src_name} → {cat}")
        text = fetch_url(session, url, fetch_timeout)

        if text is None:
            fail_count += 1
            continue

        # 按格式解析
        parser = FormatParser()
        cat_cfg = cat_configs.get(cat, {})
        prefixes = cat_cfg.get("extract_prefixes", ["DOMAIN-SUFFIX,", "DOMAIN,", "+.", "."])

        try:
            if fmt == "domain-list":
                result = parser.parse_domain_list(text, prefixes)
            elif fmt == "bare-domain":
                result = parser.parse_bare_domain(text)
            elif fmt == "ip-cidr":
                result = parser.parse_ip_cidr(text)
            elif fmt == "clash-rule-set":
                result = parser.parse_clash_rule_set(text)
            elif fmt == "surge-list":
                result = parser.parse_surge_list(text)
            elif fmt == "adguard-list":
                result = parser.parse_adguard_list(text)
            elif fmt == "hosts-list":
                result = parser.parse_hosts_list(text)
            elif fmt == "dnsmasq-conf":
                result = parser.parse_dnsmasq_conf(text)
            else:
                print(f"    [WARN] 未知格式: {fmt}, 跳过")
                continue

            raw_buckets[cat] |= result
            success_count += 1
            print(f"    → 解析到 {len(result)} 条")
        except Exception as e:
            print(f"    [ERROR] 解析失败: {e}")
            fail_count += 1

    print(f"\n  抓取结果: 成功 {success_count}, 失败 {fail_count}")

    # ---- 注入内置规则 ----
    print("\n[4/6] 注入内置规则...")
    for cat_name in CATEGORIES:
        cat_cfg = cat_configs.get(cat_name, {})
        builtin = cat_cfg.get("builtin", [])
        if builtin:
            before = len(raw_buckets[cat_name])
            raw_buckets[cat_name] |= set(builtin)
            print(f"  {cat_name}: 注入 {len(builtin)} 条内置规则 (总计 {before} → {len(raw_buckets[cat_name])})")

    # ---- 过滤与清洗 ----
    print("\n[5/6] 过滤与清洗...")
    final_buckets = {}

    for cat_name in CATEGORIES:
        raw_set = raw_buckets[cat_name]
        cat_cfg = cat_configs.get(cat_name, {})
        ma = manual.get(cat_name, {"add": [], "remove": []})

        if cat_name in ("direct_ip", "private_ip"):
            # IP 类型分类
            exclude_private = cat_cfg.get("exclude_private_ranges", False)
            filtered = filter_ip_set(raw_set, exclude_private, ma["add"], ma["remove"])
        elif cat_name == "private_ip":
            # 只保留私有 IP
            filtered = {c for c in raw_set if is_private_ip(c)}
            filtered |= set(ma["add"])
            for rm in ma["remove"]:
                filtered.discard(rm)
        else:
            # 域名类型分类
            filtered = filter_domain_set(raw_set, cat_cfg, global_excl, ma["add"], ma["remove"])
            # 后清洗：移除无效/脏域名
            filtered = clean_domain_set(filtered)

        final_buckets[cat_name] = filtered

    # ---- 生成报告 ----
    source_list = ", ".join(
        src["name"] for src in sources_cfg.get("sources", []) if src.get("enabled", True)
    )
    timestamp = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S (北京时间)")

    print("\n[6/6] 写入输出文件...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for cat_name in CATEGORIES:
        items = final_buckets[cat_name]
        # CIDR 去冗余（仅对 IP 类别）
        if cat_name in ("direct_ip", "private_ip"):
            before = len(items)
            items = compact_cidr_set(items)
            if len(items) != before:
                print(f"  [{cat_name}] CIDR 压缩: {before} → {len(items)} (移除 {before - len(items)} 条冗余)")
        # 后处理: 排序
        sorted_items = sorted(items, key=lambda x: (x.lstrip("."), x))

        output_file = OUTPUT_DIR / f"{cat_name}.txt"
        header_tpl = post_cfg.get("headers", {}).get(cat_name, f"# ProxyRules — {cat_name}")

        with open(output_file, "w", encoding="utf-8") as f:
            # 写入注释头
            header = header_tpl.format(
                timestamp=timestamp,
                count=len(sorted_items),
                sources=source_list,
            )
            f.write(header.strip() + "\n")
            # 写入规则
            for item in sorted_items:
                f.write(item + "\n")

        print(f"  {output_file.name}: {len(sorted_items)} 条")

        # 如果集合为空，也写个空文件（只含注释头）

    # ---- 总结 ----
    print("\n" + "=" * 60)
    print("完成!")

    # 打印统计
    total = sum(len(v) for v in final_buckets.values())
    stats = "\n".join(
        f"    {cat_name}: {len(final_buckets[cat_name])} 条"
        for cat_name in CATEGORIES
    )
    print(f"\n  各分类统计:\n{stats}")
    print(f"  总计: {total} 条")
    print(f"  输出目录: {OUTPUT_DIR.resolve()}")

    # 写入摘要文件 (供 CI 使用)
    summary = {
        "timestamp": timestamp,
        "total": total,
        "categories": {cat: len(final_buckets[cat]) for cat in CATEGORIES},
        "upstream_sources": source_list,
    }
    import json
    with open(OUTPUT_DIR / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
