#!/usr/bin/env python3
import requests
import re
import sys

# 目标 URL
URL_ADGUARD = "https://raw.githubusercontent.com/AdguardTeam/FiltersRegistry/master/filters/filter_11_Mobile/filter.txt"
URL_AWAVENUE = "https://raw.githubusercontent.com/TG-Twilight/AWAvenue-Ads-Rule/main/Filters/AWAvenue-Ads-Rule-hosts.txt"

# 模拟 fetch_and_filter.py 中的解析逻辑
def original_parse_adguard_line(line: str) -> str:
    # 模拟单个 adguard 行的解析
    modifiers = ["$important", "$badfilter", "$all", "$document",
                 "$popup", "$third-party", "$script", "$image",
                 "$stylesheet", "$object", "$xmlhttprequest",
                 "$subdocument", "$ping", "$websocket", "$webrtc"]
    
    # 过滤注释行
    if line.strip().startswith("#") or line.strip().startswith("!"):
        return None
        
    stripped = line.strip()
    
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
            
    stripped = stripped.strip()
    if "." in stripped and not stripped.startswith("/"):
        return stripped.lower()
    return None

def original_parse_hosts_line(line: str) -> list:
    if line.strip().startswith("#"):
        return []
    parts = line.strip().split()
    if len(parts) >= 2:
        ip = parts[0]
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip) or re.match(r'^[0-9a-fA-F:]+$', ip):
            domains = []
            for domain in parts[1:]:
                domain = domain.strip().lower()
                if "." in domain:
                    domains.append(domain)
            return domains
    return []

# clean_domain_set 逻辑
def original_clean_domain(domain_item: str) -> str:
    import re as _re
    ip_re = _re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?$')
    wildcard_body_re = _re.compile(r'^(?:\*|\+)\.(.+)$')
    
    item = domain_item.strip()
    if not item:
        return None
        
    body = item
    is_wildcard = False
    if item.startswith('*.') or item.startswith('+.'):
        wm = wildcard_body_re.match(item)
        if wm:
            body = wm.group(1)
            if not body:
                return None
            is_wildcard = True
        else:
            body = item
            
    if body.startswith('.'):
        return None
    if body.endswith('.'):
        return None
    if body.startswith('*/'):
        return None
    if body.startswith('-'):
        return None
    if ip_re.match(body):
        return None
    if any(c in body for c in (' ', ',', '(', ')', '{', '}', '[', ']', '<', '>', ';', '"', "'")):
        return None
        
    if not is_wildcard and '.' not in body:
        return None
        
    parts = body.split('.')
    tld = parts[-1]
    if tld.startswith('xn--'):
        second = parts[-2] if len(parts) >= 2 else ''
        if second.isdigit():
            return None
            
    return '+.' + body.lower()

def download_file(url: str, name: str) -> str:
    print(f"正在下载 {name} 数据源...")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"下载 {name} 失败: {e}")
        sys.exit(1)

def run_diagnose():
    adguard_data = download_file(URL_ADGUARD, "AdGuard Mobile Filter")
    awavenue_data = download_file(URL_AWAVENUE, "AWAvenue Adblock Rule")
    
    keywords = ["youtube.com", "alicdn.com"]
    
    print("\n" + "="*80)
    print("一、在 AdGuard Mobile Filter 原始数据中搜索关键词")
    print("="*80)
    
    for kw in keywords:
        print(f"\n---> 搜索含有 '{kw}' 的行:")
        found_any = False
        lines = adguard_data.splitlines()
        for idx, line in enumerate(lines, 1):
            if kw in line:
                found_any = True
                parsed = original_parse_adguard_line(line)
                cleaned = original_clean_domain(parsed) if parsed else None
                print(f"  行号 {idx:5d}: {line}")
                print(f"         └─► parse_adguard_list 解析结果: {parsed}")
                print(f"         └─► clean_domain_set   清理结果: {cleaned}")
        if not found_any:
            print("  未找到匹配行。")

    print("\n" + "="*80)
    print("二、在 AWAvenue Adblock Rule 原始数据中搜索关键词")
    print("="*80)
    
    for kw in keywords:
        print(f"\n---> 搜索含有 '{kw}' 的行:")
        found_any = False
        lines = awavenue_data.splitlines()
        for idx, line in enumerate(lines, 1):
            if kw in line:
                found_any = True
                parsed_list = original_parse_hosts_line(line)
                cleaned_list = [original_clean_domain(p) for p in parsed_list] if parsed_list else []
                print(f"  行号 {idx:5d}: {line}")
                print(f"         └─► parse_hosts_list   解析结果: {parsed_list}")
                print(f"         └─► clean_domain_set   清理结果: {cleaned_list}")
        if not found_any:
            print("  未找到匹配行。")

if __name__ == "__main__":
    run_diagnose()
