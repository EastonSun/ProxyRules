# ProxyRules

自动化代理路由规则订阅仓库，每日同步上游规则源，清洗去重后发布为 Mihomo/Shadowrocket 可用的直连域名、IP 段、广告拦截规则集。

## 功能概览

| 产物 | 格式 | 说明 |
|------|------|------|
| `direct_domain.txt` / `.mrs` | 文本 / MRS 二进制 | 中国大陆域名 + 大厂 CDN 白名单 |
| `direct_ip.txt` / `.mrs` | 文本 / MRS 二进制 | 中国大陆公网 IP 段 |
| `private_ip.txt` / `.mrs` | 文本 / MRS 二进制 | 局域网/私有/保留 IP 段 |
| `private_domain.txt` / `.mrs` | 文本 / MRS 二进制 | 局域网专用域名 |
| `reject.txt` / `.mrs` | 文本 / MRS 二进制 | 广告/追踪/统计域名拦截 |
| `Shadowrocket/direct.module` | Surge 模块 | Shadowrocket 直连模块 |
| `Shadowrocket/reject.module` | Surge 模块 | Shadowrocket 拦截模块 |

## 订阅地址

### 文本规则（适用于 Mihomo、Clash Meta）

```
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/direct_domain.txt
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/direct_ip.txt
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/private_ip.txt
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/private_domain.txt
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/reject.txt
```

### MRS 二进制规则（Mihomo 高效格式）

```
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/direct_domain.mrs
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/direct_ip.mrs
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/private_ip.mrs
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/private_domain.mrs
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/reject.mrs
```

### Shadowrocket 模块

```
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/Shadowrocket/direct.module
https://raw.githubusercontent.com/EastonSun/ProxyRules/release/Shadowrocket/reject.module
```

## 上游数据源

本仓库整合了以下社区的规则数据（排名不分先后）：

| 上游源 | 用途 |
|--------|------|
| [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules) | 直连域名、大陆 IP、广告拦截、私有 IP |
| [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat) | 增强版直连域名、广告拦截 |
| [xkww3n/Rules](https://github.com/xkww3n/Rules) | 中日广告过滤、国内域名 |
| [ACL4SSR/ACL4SSR](https://github.com/ACL4SSR/ACL4SSR) | 经典中国域名与广告拦截 |
| [Cats-Team/AdRules](https://github.com/Cats-Team/AdRules) | 中国区广告规则合集 |
| [zqzess/rule_for_quantumultX](https://github.com/zqzess/rule_for_quantumultX) | 海量广告域名 |
| [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script) | 全面按服务细分的规则库 |
| [felixonmars/dnsmasq-china-list](https://github.com/felixonmars/dnsmasq-china-list) | 中国域名白名单 |
| [gaoyifan/china-operator-ip](https://github.com/gaoyifan/china-operator-ip) | 中国运营商 IP 段 |
| [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) | Mihomo 官方生态规则 |

## 自动化流水线

```mermaid
graph TD
    A[北京时间 08:00<br/>GitHub Actions 触发] --> B[fetch_and_filter.py<br/>抓取 10 个上游源]
    B --> C{分类去重}
    C --> D[direct_domain.txt]
    C --> E[direct_ip.txt]
    C --> F[private_ip.txt]
    C --> G[private_domain.txt]
    C --> H[reject.txt]
    D --> I[compile_mihomo.py<br/>→ .mrs 二进制]
    E --> I
    F --> I
    G --> I
    H --> I
    D --> J[generate_sr.py<br/>→ .module]
    H --> J
    I --> K[发布到 release 分支]
    J --> K
```

## 手动自定义规则

仓库提供四个手动干预文件，你可以直接修改后提交，下次构建时自动生效：

| 文件 | 作用 |
|------|------|
| `config/add_direct.txt` | 追加直连域名（一行一个） |
| `config/remove_direct.txt` | 从直连名单中删除（一行一个） |
| `config/add_reject.txt` | 追加拦截域名 |
| `config/remove_reject.txt` | 从拦截名单中删除 |

## 本地运行

```bash
# 1. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r scripts/requirements.txt

# 3. 抓取并清洗规则
python scripts/fetch_and_filter.py

# 4. 编译 Mihomo MRS 二进制（需要本地安装 mihomo）
python scripts/compile_mihomo.py

# 5. 生成 Shadowrocket 模块
python scripts/generate_sr.py
```

## 发布流程

1. Fork 本仓库
2. 创建一个空的 `release` 分支：`git checkout --orphan release && git commit --allow-empty -m "init" && git push origin release`
3. 在 Settings → Actions → Workflow permissions 中选择 **Read and write permissions**
4. 手动触发一次 Actions（`workflow_dispatch`）验证
5. 后续每天北京时间 08:00 自动更新

## 许可

MIT License
