# Anti-Blocking: Clash YAML 域名被 GFW 墙时自动换 Host（server 不动）

## 1. 目标

对 GitHub 仓库 `<REDACTED_GH_REPO>` 的 `nodes/ms/az.yml` 实现自动化：

1. 检测当前使用的伪装 Host 域名（`<REDACTED_HOST>`）是否被 GFW 阻断；
2. 被墙时，在 CF 托管的 zone `<REDACTED_DOMAIN>` 下新建子域（DNS CNAME → 隧道路由），确保 **CF 边缘能把新 Host 路由到 cloudflared 隧道**；
3. 批量改写 `az.yml` 里 19 个 proxy 的 `ws-opts.headers.Host`（`server` 为 CDN 接入点，保持不动）；
4. 用 GitHub PAT 通过 Contents API 推送新文件；
5. 拉回验证连通性。

## 2. 已确认事实（勘探结论）

### 2.1 Clash YAML 结构（az.yml, 3772 行）
- 19 个 proxy，**全部同构**：
  ```
  - {name: <任意>, server: <CDN/CF 域名>, port: 2095, client-fingerprint: randomized,
     type: vless, uuid: 86c50e3a-5b87-49dd-bd20-03c7f2735e40, tls: false, tfo: false,
     skip-cert-verify: false, network: ws,
     ws-opts: {path: /, headers: {Host: <REDACTED_HOST>}}}
  ```
- `server` 是各种 CDN / Cloudflare 域名（`cloudflare-dns.com`、`www.epicgames.com`、`blog.cloudflare.com`…），本质=边缘接入点；
- **真正决定路由到 vless 后端的是 `ws-opts.headers.Host`**；
- port=2095，tls=false ⇒ 明文 HTTP/1.1 + WS，无 SNI，只有 Host 头。

### 2.2 后端链路（重要）
- vless 服务端运行在 **GitHub runner** 上，通过 **cloudflared Argo Tunnel** 暴露；
- **隧道 UUID = `<REDACTED_TUNNEL_UUID>`**（tunnel name: `<REDACTED_TUNNEL>`，account: `<REDACTED_CF_ACCOUNT>`）；
- CF 侧 DNS：`<REDACTED_HOST>` 是
  **CNAME → `<REDACTED_TUNNEL_UUID>.cfargotunnel.com`（proxied=True, ttl=1）**，
  CNAME 最终解析到 CF 边缘任意播 IP `104.21.x / 172.67.x`；
- 请求链路：Clash 客户端 → `server`(解析到 CF 边缘 IP) → CF 边缘(**按 Host 匹配隧道 public hostname**) → Argo Tunnel → runner 本地 vless/ws 服务。
- **"给隧道添加路由" = 两段动作**：① DNS 记录指向 cfargotunnel.com；② 隧道 **public hostname** 列表里有该 hostname（否则 CF 边缘返回 404/502）。

### 2.3 分层检测实测基准（2026-09-25 实测，旧域名 <REDACTED_DOMAIN> 时期）
| 域名 | Layer1 HTTP(80) | Layer2 WS(80/2095) | Layer3 干净IP(10099) 上Host头响应 | 判定 |
|---|---|---|---|---|
| `<REDACTED_HOST>` | **`400`** + `sec-websocket-version:13` | **`101`** | ✅ 403（<REDACTED_IP> 曾 5/5 success_all） | **全链路健康** |
| `<REDACTED_HOST>` | **`400`** + `sec-websocket-version:13` | **`101`** | ❌ **`tcp_rst`**（<REDACTED_IP>，同 IP 上 epicgames/museai 均 403） | **Host 被 GFW 封锁** |
| `testtunnel.femboy.us.ci` | **`502`**(CF 隧道错误) | `502` | — | 对照组（其它隧道/状态异常） |

> **注**：上表为 2026-09-25 在旧域名 `<REDACTED_DOMAIN>` 下的实测基准。当前已迁移到 `<REDACTED_DOMAIN>`，CHECK_HOST = `<REDACTED_HOST>`。检测逻辑不变，实测基准待用新域名重跑确认。

**关键交叉验证（排除 IP 因素）**：`<REDACTED_IP>`（中国移动 CDN，epicgames 哨兵有 403 响应 = 干净通道）上，
`museai` 与 `epicgames` 均收到 HTTP 状态码，**唯独 `llm-api` RST** ⇒ 是 Host 头的 GFW 双向审查命中，非 IP 问题。

### 2.4 CF 账户与权限（2026-10-07 更新）

**当前双 CF 账户配置**：

| 账户 | 用途 | Account ID | API Token | Zone | Zone ID | Tunnel UUID |
|---|---|---|---|---|---|---|
| `<REDACTED_CF_ACCOUNT>` | **主账户（Tunnel A 业务面）** | `<REDACTED_ACCOUNT_ID>` | `<REDACTED>…` | `<REDACTED_DOMAIN>` | `<REDACTED_ZONE_ID>` | `<REDACTED_TUNNEL_UUID>` |
| `<REDACTED_CF_ACCOUNT>` | **备用账户（L2 failover）** | `<REDACTED_ACCOUNT_ID>` | `<REDACTED>…` | `<REDACTED_DOMAIN>` | `<REDACTED_ZONE_ID>` | `<REDACTED_TUNNEL_UUID>` |

**API Token 权限要求（两账户均需）**：

| # | 授权范围 | 权限 | 用途 |
|---|---|---|---|
| 1 | 账户内**所有区域** | **`DNS` → `Write`** | 轮换子域 CNAME 增删：`{sub}.{zone}` → `{TID}.cfargotunnel.com`（proxied, ttl=1） |
| 2 | 整个**账户** | **`Cloudflare One Connectors` → `Write`** | 隧道配置读写：`cfd_tunnel` 列表/详情 + `PUT configurations`（把新子域插入 ingress） |

- Token 经 `.env` 提供（chmod 600），勿入仓库。
- 主账户 tunnel `<REDACTED_TUNNEL>`（<REDACTED>）healthy，8 连接。
- 备用账户 tunnel `<REDACTED_TUNNEL>`（<REDACTED>）已创建但 **tunnel connector token secret 丢失**，需删除重建或通过 CF API 获取（见 §12.9 待办）。

### 2.5 可复用资产
- `/home/ubuntu/myscript/GFWcheck/domain_cdn_check.py`：**Layer3 核心逻辑**——从中国境内 CDN IP（10099 移动 A 记录）做 sentinel(`www.epicgames.com`@80) 预检得"干净通道 IP 池"，再对这些 IP 带目标 Host 头发 HTTP 请求，按有无 HTTP 响应/ RST / 超时分类。
- 服务器现成工具：`cloudflared`、`jq`、`python3`、PyYAML 6.0.1、requests 2.31.0。

### 2.6 缺口
- GitHub PAT 已提供（`<REDACTED_PATH>`，待 chmod 600 + 验证写权限）。
- ~~token 缺 DNS:Edit~~ → **已解决**（2026-09-25 已加所有区域 DNS:Write）。
- **备用 CF 账户 tunnel connector token 丢失**（创建时随机生成 secret 未保存）→ 需删除 tunnel <REDACTED> 重建，或通过 CF API 获取 token（见 §12.9）。

## 3. 分层检测逻辑（用户定义，最终版）

> **Layer1 = 检测 HTTP 状态码**：400+ws_ver ⇒ Github 侧正常；502 ⇒ runner/origin 问题（L1 重建）；403 ⇒ CF 侧问题如 CF Access 拦截（L2 切隧道）；
> **Layer2 = 检测 WS 是否 101**（判断 Cloudflare 这边有没有限制）；
> **Layer3 = Host 是否被 GFW 封锁**（中国境内干净 CDN IP + Host 头探测，双向审查原理）。

```
┌─ Layer 1  HTTP 检测（Github 侧）—— 判据：状态码是否 == 400
│    curl http://<CHECK_HOST>/      （80 端口明文，可加 2095 并行）
│    ├─ 状态码 == 400(带 sec-websocket-version:13 特征) → GITHUB_OK → 进 Layer2
│    ├─ 状态码 == 403 → CF_WS_BLOCKED（CF Access 拦截等 CF 侧问题）
│    │      → 触发 L2 remediation（切备用隧道 az.yml Host A→B，见 §12.4）
│    └─ 其它非 400 (502/2xx/404/连接失败/超时/RST) → GITHUB_DOWN（runner/origin 问题）
         → 记录日志，L1 remediation 已移交 Oracle Agent persistent loop
│
├─ Layer 2  WS 检测（Cloudflare 网络侧）—— 判据：是否收到 101
│    socket 发 ws Upgrade 握手（Upgrade: websocket + Sec-WebSocket-Version: 13）
│    ├─ 收到 101 Switching Protocols → CLOUDFLARE_OK
│    │      Layer1+Layer2 全通 = 隧道与 CF 网络正常 → 进 Layer3
│    └─ 非 101 (400/403/502/超时/RST) → CLOUDFLARE_BLOCKED
         = Tunnel A WS 故障 → 检测 Tunnel B WS → B OK → L2 remediation（az.yml Host A→B，见 §12.4）
│
└─ Layer 3  Host 被墙检测（GFW 双向审查原理，用户定义）
       ├─ 步骤A sentinel 预检：中国境内 CDN IP（10099 移动 A 记录）上，
       │     以 www.epicgames.com 为 Host 头发 GET / ，能收到 HTTP 响应
       │     = 该 IP 是"干净通道"（GFW 未干扰、能正常出入境的 IP 路径）
       ├─ 步骤B 用所有这些干净 IP，对 <CHECK_HOST> 发 GET /（Host 头=自己域名）
       │     ├─ 收到 HTTP 状态码（403/400/502 等任何状态码）→ 未墙 → 全链路健康，退出
       │     └─ RST / 超时 → GFW 双向审查命中 = Host 被墙
       │          → 触发 L3 remediation（换 Host，见 §4）
       └─ 原理：GFW 对明文 HTTP 的 Host 头/SNI 做双向审查（入/出境都匹配封锁名单），
            故从境外服务器向中国境内 CDN IP 发 Host 头请求，被墙域名照样触发 RST，
            境外也能测出域名是否被 GFW 封锁。
```

**判定矩阵**：
| Layer1 HTTP(==400?) | Layer2 WS(==101?) | Layer3 干净IP上有HTTP响应? | 结论 | 动作 |
|---|---|---|---|---|
| ✅ | ✅ | ✅ | 全链路健康 | 退出 |
| ✅ | ✅ | ❌ 全部RST/超时 | **Host 被 GFW 封锁** (L3) | 换 Host（建 DNS→加隧道路由→重写→推送，server 不动） |
| ✅ | ❌ | — | Tunnel A WS 故障 (L2) | 检测 Tunnel B；B OK → az.yml Host A→B（见 §12.4） |
| 403 | — | — | CF WS 被拦截 (L2) | CF Access 拦截等 CF 侧问题 → az.yml Host A→B（见 §12.4） |
| 502/其它 | — | — | Runner 故障 (L1) | 重建 Runner，atlas1 同时部署 A+B（见 §12.4） |
| ? | ? | ? | 检测矛盾/超时 | **inconclusive**：不修改 az.yml，记录日志，下周期重试 |

## 4. 方案架构（双 Tunnel 常驻 + 三层故障切换）

### 4.0 架构总览

```
                    Oracle Runner
                         │
              ┌──────────┴──────────┐
              │                     │
        Tunnel A 常驻            Tunnel B 常驻
        主业务 Tunnel             备用业务 Tunnel
        cloudflared.service       cloudflared-biz.service
              │                     │
        Host A / Zone A          Host B / Zone B
        <REDACTED_DOMAIN>       <REDACTED_DOMAIN>
              │                     │
              └──────────┬──────────┘
                         │
                    az.yml
                         │
                  当前只指向 A 或 B
```

> Tunnel A 和 Tunnel B 均为 Runner 上常驻运行的业务 Tunnel。A 为主，B 为备用。
> atlas1 模板 Step 4 部署 Tunnel A，Step 6 部署 Tunnel B，两者同时启动。

### 4.1 三层故障切换状态机

| 状态 | Runner | current tunnel | backup tunnel | 动作 |
|------|--------|----------------|---------------|------|
| **正常** | ✅ | ✅ | ✅ | 使用 current tunnel（az.yml Host 指向 current 的域名） |
| **L1** | ❌ | ❌/未知 | ❌/未知 | 重建 Runner，atlas1 同时恢复 A+B |
| **L2** | ✅ | ❌ | ✅ | az.yml Host: current → backup，swap(backup→current) |
| **L3** | ✅ | ✅ | ✅ | current tunnel 的 Zone 内创建新 Host，az.yml Host: 旧 → 新 |
| **双 Tunnel 都故障** | ✅ | ❌ | ❌ | 告警，等待人工处理 |
| **L3 预检失败** | ✅ | ✅ | ✅ | 不修改 az.yml，记录失败，下一周期重试 |
| **inconclusive** | ? | ? | ? | 检测结果矛盾或超时，不修改 az.yml，记录日志，下一周期重试 |

> **current / backup 动态角色**：初始 current=A, backup=B。L2 触发后 swap：current=B, backup=A。
> 下次 L2 可以 B→A，不写死 A→B。
> **L2 和 L3 正交**：可以先 L2 切到 B，再 L3 在 B 的 Zone 创建新 Host（B1 → B2）。
> 每个 Tunnel 可以有多个 Host（A1/A2/A3、B1/B2/B3），az.yml 永远只在这些 Host 之间切换。

### 4.2 三层职责定义

**L1：Runner 故障**
```
Runner DOWN
    ↓
重新创建/拉起 Runner（调 Oracle Agent API）
    ↓
atlas1 模板部署
    ├── Tunnel A (cloudflared.service)
    └── Tunnel B (cloudflared-biz.service)
```

**L2：current tunnel 故障（backup tunnel 正常）**
```
Runner OK, current WS FAILED, backup WS OK
    ↓
修改 az.yml: ws-opts.headers.Host: current Host → backup Host
    ↓
push GitHub
    ↓
verify backup（HTTP 400 + WS 101）
    ↓
verify OK → swap(current, backup)，完成
verify FAIL → 检查原 current 是否恢复
    ├─ current 恢复 → rollback: backup Host → current Host，push，保持 current 不变
    └─ current 也失败 → 保持当前状态，告警，等待人工处理
```
> **不做**：修改 atlas1 secret、删除 Tunnel、重新部署 Runner、修改 server。
> 只修改 `ws-opts.headers.Host`，这是最快的故障切换路径。
> **rollback 原则**：不盲目来回切。B 验证失败时先检查 A 是否恢复，A 恢复则回切 A，A 也挂则保持现状告警。

**L3：Host 被封（Tunnel 本身正常）**
```
Runner OK, Tunnel WS OK, Host BLOCKED
    ↓
active = get_active_tunnel()  ← 从 az.yml 当前 Host 反查所属 Tunnel
    ↓
在 active tunnel 的 CF Zone 创建新子域名（如 opencode-api-2.{active_zone}）
    ↓
候选 Host GFW 预检（clean CDN IP + Host header，无 DNS 状态下探测）
    ↓
GFW 预检通过 → 创建 DNS CNAME → active tunnel UUID + tunnel ingress
    ↓
预检测 HTTP 400 + sec-websocket-version:13 特征（严格匹配，非泛 400）
    ↓
预检通过 → 修改 az.yml Host: 旧 → 新
预检失败 → 不修改 az.yml，记录失败，下一周期重试
```
> L3 根据 active tunnel 动态选择 CF 账户/zone/tunnel UUID，不永远默认操作主账户。
> active == A → A API token + A zone + A tunnel UUID
> active == B → B API token + B zone + B tunnel UUID
> L3 仍然使用当前 Tunnel，不需要切到另一个 Tunnel。

### 4.3 L3 切换流水线（详细步骤）

```
cron / tg-agent 手动
      │
      ▼
cf_ip_rotate.py --check-only
      │
      ├─ 0. 分层检测 (Layer1 HTTP==400? → Layer2 WS==101? → Layer3 Host 被墙?)
      │        全健康 → 退出
      │        Layer1 非400（Runner 挂）→ 记录日志，由 Oracle Agent persistent loop 处理
      │        Layer2 非101（Tunnel A WS 挂）→ 检测 Tunnel B WS
      │             Tunnel B OK → L2 remediation（az.yml Host A→B，见 §12.4）
      │             Tunnel B 也挂 → 告警，等待人工处理
      │        Layer3 blocked（Host 被 GFW 墙）→ L3 remediation（下方流水线）
      ├─ 1. fetch az.yml (raw.githubusercontent.com)
      ├─ 2. extract 19 proxies (server/port/uuid/tls/network/ws Host/path)
      ├─ 3. find_new_subdomain: 递增 opencode-api-N.{当前zone}，三重检查取第一个可用者
      │        ① CF DNS 记录不存在  ② CF Tunnel ingress 中不存在  ③ GFW precheck 通过（clean IP + Host header 探测）
      │        rotate_history.json 不参与可用性判定（仅审计，见 §4.5）
      ├─ 4. create_dns_record: **CNAME → {当前TUNNEL_UUID}.cfargotunnel.com（proxied=true, ttl=1）**
      │        （预检：DNS 生效后 curl https://新域/ 应 400，非 404 —— 若 404 说明隧道 public hostname 未含新域）
      ├─ 5. rewrite_yaml: Host=新域名，server 保持不动，其余不动
      │        校验：parse YAML → 确认 proxies==19 → 记录 before_server_hash
      │        → 精确文本替换 Host → parse YAML → 确认 19/19 Host==新域名
      │        → 确认 after_server_hash==before_server_hash → port/uuid/tls/network/path 不变
      ├─ 6. push_github: GET sha → PUT contents
      ├─ 7. verify: 重拉 raw diff + 新 Host 重跑 Layer1/Layer2/Layer3
      └─ 8. 记录 rotate_history.json（审计日志）+ rotate.log
```

### 4.4 设计约束（不可违反）

**所有 remediation 的唯一客户端配置变更**：
- ✅ 允许修改：`ws-opts.headers.Host`（19 个 proxy 全部替换为新域名）

**禁止修改**：
- ❌ `server`（CDN/CF 边缘接入点，保持不动）
- ❌ `port`、`uuid`、`tls`、`tfo`、`skip-cert-verify`、`network`
- ❌ `ws-opts.path`
- ❌ `client-fingerprint`
- ❌ 任何其他 proxy 参数

> 本文档标题即原则：**server 不动**。不存在"优选 IP / server rotation"路线。
> `SERVER_MODE` 和 `probe_cf_ips` 已从设计中彻底删除。
> L1/L2/L3 三层故障切换中，只有 L1 重建 Runner 时会触碰 atlas1 secret；L2/L3 只改 az.yml 的 Host。


### 4.5 hostname 可用性判定：CF 为事实来源 + GFW 预检（2026-10-07 重构）

**设计原则：Cloudflare 是 source of truth，本地 history 是审计日志。**

#### 旧逻辑（已废弃）

```
find_new_subdomain:
  查 rotate_history.json（本地文件）→ 用过的名字不复用
  查 CF DNS 记录 → 不存在则可用
```

问题：`rotate_history.json` 被删除 / `.env` 被重建 / 脚本换机器 / 手工创建过域名 / Tunnel 配置与本地历史不一致 → 都可能导致冲突。

#### 新逻辑

```
find_new_subdomain(active, prefix):
  ① CF DNS 记录不存在        ← list_dns_records(zone_id, token, name=candidate)
  ② CF Tunnel ingress 不存在  ← list_tunnel_ingress_hostnames(account_id, tunnel_id, token)
  ③ GFW precheck 通过         ← gfw_host_precheck(candidate)
  三项全过 → 返回候选域名
```

**①② 占用判断 = CF 实际状态（DNS + Tunnel routes）**，不依赖本地文件。
即使 `rotate_history.json` 被删除、脚本换机器、手工创建过域名，都不会冲突。

**③ GFW 预检：创建资源前先验证候选 Host 字符串本身是否被网络侧拦截。**

技术细节：候选 Host 此时尚无 DNS 记录，利用 L3 的 "指定 IP + Host header" 机制——
向 clean CDN IP 发 HTTP 请求，Host 头设为候选域名，检查 GFW 对该 Host 字符串的行为。
有 HTTP 响应 → 候选 Host 可用；全部 RST/timeout → 疑似被墙，跳过该候选。

**两阶段检查不可互相替代：**
- 创建前 GFW check：验证"Host 名字本身有没有被网络侧拦截"
- 创建后 HTTP/WS preflight：验证"Host 已真正接入 CF Tunnel 且业务链路正常"

**`rotate_history.json` 降级为审计日志**：`record_history()` 仍记录每次切换（old_host → new_host, reason, timestamp），但不参与 hostname 可用性判定。

## 5. 目录 / 文件结构

```
/home/ubuntu/antiblocking/
├── PLAN.md               ← 本文件（设计文档）
├── .env.example          ← 配置模板（不含真实 token）
├── .env                  ← 真实 token（chmod 600，不入库）
├── cf_ip_rotate.py       ← 主脚本（纯 stdlib + requests + pyyaml，风格对齐 GFWcheck）
├── rotate_history.json   ← 审计日志（切换历史，不参与可用性判定，见 §4.5）
└── rotate.log            ← 运行日志（追加）
```

## 6. 配置模板 `.env`

```bash
# === 主 CF 账户（Tunnel A 业务面，<REDACTED_DOMAIN>）===
CLOUDFLARE_API_TOKEN=<REDACTED>***（主账户 token）
CLOUDFLARE_ZONE_ID=<REDACTED_ZONE_ID>
CLOUDFLARE_ZONE_NAME=<REDACTED_DOMAIN>
CLOUDFLARE_ACCOUNT_ID=<REDACTED_ACCOUNT_ID>
CLOUDFLARE_TUNNEL_UUID=<REDACTED_TUNNEL_UUID>
CLOUDFLARE_NEW_SUBDOMAIN_PREFIX=opencode-api

# === 备用 CF 账户（L2 failover，<REDACTED_DOMAIN>）===
CLOUDFLARE_API_TOKEN_BACKUP=<REDACTED>***（备用账户 token）
CLOUDFLARE_ZONE_ID_BACKUP=<REDACTED_ZONE_ID>
CLOUDFLARE_ZONE_NAME_BACKUP=<REDACTED_DOMAIN>
CLOUDFLARE_ACCOUNT_ID_BACKUP=<REDACTED_ACCOUNT_ID>
CLOUDFLARE_TUNNEL_UUID_BACKUP=<REDACTED_TUNNEL_UUID>
# 注：B connector token 不存 .env，只存 atlas1 cf_tunnel_b（见 §13.0）

# === GitHub（PAT 走文件，需 repo 权限，写 <REDACTED_GH_REPO> 的 contents）===
GITHUB_PAT_FILE=<REDACTED_PATH>
GITHUB_REPO=<REDACTED_GH_REPO>
GITHUB_FILE=nodes/ms/az.yml
GITHUB_BRANCH=main
GITHUB_COMMITTER_NAME=github-actions[bot]
GITHUB_COMMITTER_EMAIL=41898282+github-actions[bot]@users.noreply.github.com

# === Oracle Agent（L2 remediation 用）===
ORACLE_AGENT_API_URL=http://127.0.0.1:8700
ORACLE_ADMIN_TOKEN=***（/etc/default/oracle-agent 中的 ORACLE_ADMIN_TOKEN）
ORACLE_TEMPLATE_NAME=atlas1

# === 行为 ===
CHECK_HOST=<REDACTED_HOST>   # 当前被监控的伪装 Host
REMEDIATION_COOLDOWN_S=300            # remediation 冷却/防抖窗口（秒），两次切换最小间隔
```

## 7. cf_ip_rotate.py 函数清单

| 函数 | 说明 |
|---|---|
| `load_env()` | 读 .env（缺失 CF token 时明确报错退出） |
| `load_pat()` | 按 `GITHUB_PAT_FILE` 读取 GitHub PAT（gh_pat.txt，chmod 600） |
| `fetch_yaml(url)` | 拉取 az.yml（UA 跟随重定向，同现有脚本） |
| `extract_proxies(text)` | 解析 19 个 proxy，返回结构化列表 |
| `layer1_http_check(host)` | **仅测 HTTP，判据=状态码==400**：`400+sec-websocket-version:13`=>GITHUB_OK；其余一律 GITHUB_DOWN |
| `layer2_ws_check(host)` | **才测 WS**：收到 `101` 立即判成功并关连接；非 101/超时/RST=>CLOUDFLARE_BLOCKED |
| `layer3_host_blocked_check(host)` | **Host 被墙检测**：①10099 移动 CDN IP 池 sentinel(epicgames@80) 预检得"干净 IP 列表"；②干净 IP 上带目标 Host 发 GET /；③任一收到 HTTP 状态码=>未墙；全部 RST/超时=>**blocked** |
| `diagnose(host)` | 按序 Layer1→Layer2→Layer3，返回 `healthy / runner_down / cf_ws_blocked / host_blocked` |
| `cf_api(method, path, body=None, token=None)` | CF API 封装（base `https://api.cloudflare.com/client/v4`），token 默认用主账户，可传入备用账户 token |
| `list_dns_records(zone_id, name=None)` | 查 DNS 记录 |
| `list_tunnel_ingress_hostnames(account_id, tunnel_id, token)` | 查 CF Tunnel configurations，返回 ingress 中已配置的 hostname 集合 |
| `gfw_host_precheck(host)` | 无 DNS 状态下用 clean CDN IP + Host header 探测候选 Host 是否被墙。有 HTTP 响应=可用，全 RST=被墙 |
| `find_new_subdomain(active, prefix)` | 递增 `opencode-api-N`，三重检查取第一个可用者：① CF DNS 不存在 ② Tunnel ingress 不存在 ③ GFW precheck 通过。`rotate_history.json` 不参与判定（仅审计） |
| `create_dns_record(zone_id, name, tunnel_uuid)` | 建 **CNAME → {tunnel_uuid}.cfargotunnel.com**（proxied=true, ttl=1） |
| `add_tunnel_ingress(account_id, tunnel_id, hostname, token)` | PUT configurations，把新 hostname 插入 ingress → `http://localhost:11451` |
| `rewrite_yaml(text, new_host)` | 纯文本替换 Host 字段 + 结构校验：parse before/after YAML，确认 proxies==19、19/19 Host==new、server_hash 不变、port/uuid/tls/network/path 不变 |
| `push_to_github(text, sha)` | Contents API PUT，committer=GitHub bot |
| `verify(host)` | 重拉 raw 对比 sha + 对新 Host 重跑 Layer1/Layer2/Layer3 |
| **L3 remediation** | `get_active_tunnel → find_new_subdomain(DNS+Tunnel+GFW precheck) → create_dns_record → add_tunnel_ingress → rewrite_yaml → push_to_github → verify`，根据 active tunnel 动态选择 CF 账户/zone |
| **L1 remediation** | **已移除**（2026-10-09）。L1 检测保留（diagnose 返回 runner_down 并记日志），remediation 交由 Oracle Agent persistent loop 处理（每 60s 巡检，带 template+nonce 正确 dispatch，支持 atlas1+atlas2） |
| **L2 remediation** | ① 检测 Tunnel B WS（curl Host B → 期望 101）② rewrite az.yml: ws-opts.headers.Host 全部改为 Host B ③ push GitHub ④ verify。**不重建 Runner、不改 atlas1 secret、不改 server** |
| `main()` | argparse: `--check-only / --dry-run / --force-host X` |

## 8. 触发方式
1. **crontab**（ubuntu）：每 5 分钟 `*/5 * * * * python3 cf_ip_rotate.py --check-only >> rotate.log`；
2. **tg-agent 手动**：接入 MCP runner 手工执行（可选，二期）。

## 9. 里程碑验证（实施顺序）

1. [ ] 建目录/.env 占位 → `--check-only` 跑通分层检测（当前 CHECK_HOST=<REDACTED_HOST> 应 healthy）；
2. [ ] `--dry-run`：输出新 yaml 与旧版 diff 预览（不建 DNS、不推送）；
3. [ ] 手动切一次：建 CNAME → 预探 400 → 推 GitHub → verify（重拉 sha + 新 Host 重跑三层）；
4. [ ] 装 crontab（`*/5` 每 5 分钟），观察一轮日志。

## 10. 已确认 / 待办
- [x] CF token 能力已测：**Cloudflare One Connectors:Write ✅ + 所有区域 DNS:Write ✅**（双账户均配齐）；
- [x] **隧道 public hostname 自动添加已打通**：configurations PUT 即可；
- [x] GitHub PAT 已提供：`<REDACTED_PATH>`；
- [x] 新子域名前缀：**`opencode-api-N` 递增**；
- [x] 检测周期：**每 5 分钟**；
- [x] GitHub 提交者：**GitHub bot**；
- [x] Layer1=400(Github侧)；Layer2=101(CF限制)；Layer3=sentinel干净IP+Host头探测(GFW双向审查)；
- [x] **双 CF 账户已配置**：主(<REDACTED>) + 备(<REDACTED>)，L2 failover 有明确目标；
- [x] **Oracle Agent multi-PAT failover 已实现**：<REDACTED_USER>(主) + <REDACTED_ACCOUNT>(备)，L1 failover 复用现有逻辑；
- [ ] **atlas1 模板 cf_tunnel_a secret 需更新**：当前仍为旧 token（account `<REDACTED>...`, tunnel `<REDACTED>-...`），需换为当前 tunnel connector token（见 §12.3）；
- [ ] **备用 CF 账户 tunnel connector token 丢失**：tunnel <REDACTED> 创建时 secret 未保存，需删除重建（见 §12.9）；
- [ ] **两个明文 token 已出现在聊天记录，集成完成后转子**。

## 11. 注意事项
- **Layer1 三路判定**：400+ws_ver → Github 正常（进 L2）；403 → CF 侧问题（CF Access 拦截等，触发 L2 切隧道）；502/其它非 400 → runner/origin 问题（触发 L1 重建）；
- **Layer2 只看 101**：收到 101 立即判成功，101 后裸 ws 无 HTTP 字节导致超时属正常，不得当作失败；
- **Layer3 判定注意**：只要任一干净 IP 上目标 Host 收到 HTTP 状态码（403/400/502 都算）即"未墙"；状态码内容不重要，**只看 有无响应 vs RST/超时**；
- **新域必须走 CNAME→cfargotunnel.com（proxied=true, ttl=1）**，与现有记录一致；建后预探非 404 才算隧道路由成功；
- **L3 轮换主路径已闭环**：DNS CNAME → PUT configurations 插新子域 → 探测 400 → 改 az.yml 推 GitHub → verify；
- **L1 复用 Oracle Agent 现有能力**：multi-PAT failover 已在 gh.py 实现，cf_ip_rotate.py 只需调 admin API trigger + exec；
- **L2 需要备用 tunnel connector token**：当前丢失，是 L2 的唯一阻塞项；
- 切换历史 json 必须保留 旧Host/新Host/IP/时间，便于回滚；
- **四个状态互斥且必须分开**：runner_down / cf_ws_blocked / host_blocked / healthy。

---

## 12. atlas1 / atlas2 模板 —— L1/L2 备用部署通道（oracle-agent task template）

### 12.1 背景与定位
- 主链路 `cf_ip_rotate.py` 只处理 `host_blocked`（L3：换 Host + 隧道路由 + 推 GitHub），但 **`runner_down`（L1：GitHub/runner 侧挂）与 `cf_ws_blocked`（L2：Cloudflare 网络层限制）换 Host/IP 均无效**。
- 备用手段 = **用全新 GitHub runner 重建整套业务面 + 控制面**：`atlas1` / `atlas2` 两个模板（均已导入 oracle.db `task_templates`，`enabled`，6 步，`secrets=(cf_tunnel_a, cf_tunnel_b)`）。控制面 = 外部 `atlas1.service` / `atlas2.service`（从 GitHub 拉取），业务面 = Tunnel A + Tunnel B 两条 cloudflared。**两个模板共用业务面隧道，独立控制面服务（见 §12.7）。**
- 模板引擎特性（重要）：
  - `run` 必须是 **argv 数组**（exec 直接执行，非 shell 字符串拼接）；
  - `env` 值为 `{secret: REF}` 格式，引用 `template_secrets` 表中的 key；
  - **当前 token 通过 template_secrets 注入**（`cf_tunnel_a` / `cf_tunnel_b`），非明文硬编码在 run 内；
  - L2 语义 = 编辑 secret 值 → re-apply（`ctl.py template set-secret atlas1 cf_tunnel_a <new_token>`），或编辑 YAML → re-import → re-apply。

### 12.2 atlas1 模板 6 步（2026-10-07 确认）
| # | 步骤名 | 超时 | 内容 |
|---|---|---|---|
| 1 | Show IP info | 120s | `curl ifconfig.io`；`chpasswd` 设 runner 密码；`systemctl start ssh` |
| 2 | Download and setup Xray | 600s | Xray-linux-64.zip → `/usr/local/bin/xray`；拉 <REDACTED_USER> `config.json` + `xray.service`；`xray -test` |
| 3 | Start Xray service | 180s | daemon-reload + enable + start + status |
| 4 | Install latest cloudflared (tunnel A via secret) | 600s | cloudflared → `/usr/local/bin`；**Tunnel A** `service install "$TUNNEL_A_TOKEN"` → `cloudflared.service`（主业务隧道）；拉 `atlas1.service`（**控制面**：runner 管理/shell-mcp/tgbot）→ enable+start。`env: {TUNNEL_A_TOKEN: cf_tunnel_a}` |
| 5 | debug | 900s | mcp-venv(mcp==2.2.0)+shell-mcp；tgbot（`BOT_TOKEN=8851973104:...` / `CHAT_IDS=749724296` 明文 sed 注入 tgbot.service）；enable+start |
| 6 | Install cloudflared biz connector | 600s | **Tunnel B** `cloudflared tunnel run --token "$BIZ_TOKEN"` → `cloudflared-biz.service`；enable+start+is-active 校验。`env: {BIZ_TOKEN: cf_tunnel_b}` |

### 12.3 双隧道拓扑（关键约束，勿混淆）
| 隧道 | 平面 | CF 账户 | tunnel UUID | 服务 | token 来源 | 当前状态 |
|---|---|---|---|---|---|---|
| **Tunnel A** | 业务面：vless/xray 流量，= 当前 az.yml `Host=<REDACTED_HOST>` 所在隧道 | `<REDACTED_CF_ACCOUNT>` (`<REDACTED_ACCOUNT_ID>`) | `<REDACTED_TUNNEL_UUID>` | `cloudflared.service` | atlas1/atlas2 step4 `cf_tunnel_a` secret | ✅ 已更新（2026-10-07，当前 tunnel connector token） |
| **Tunnel B** | **备用业务隧道**：与 Tunnel A 同跑 VLESS 流量，不同 CF 账户/tunnel 做冗余。L2 failover 切换目标 | `<REDACTED_ACCOUNT_ID>`（<REDACTED_CF_ACCOUNT>） | `<REDACTED_TUNNEL_UUID>` | `cloudflared-biz.service` | atlas1/atlas2 step6 `cf_tunnel_b` secret | ✅ 已更新（2026-10-07），L2 触发时切换 |

> **✅ 已完成（2026-10-07）**：atlas1 + atlas2 模板的 `cf_tunnel_a` / `cf_tunnel_b` secret 均已更新为当前 tunnel connector token。

**Tunnel A connector token 解码**（base64 → JSON）：
```json
{
  "a": "<REDACTED_ACCOUNT_ID>",   ← <REDACTED_CF_ACCOUNT>
  "t": "<REDACTED_TUNNEL_UUID>", ← tunnel <REDACTED_TUNNEL>
  "s": "OTk5MjVlZDUtYWQyOT00MmFjLTgwNWEtMWIyM2I2ZmE3ZWM0"  ← tunnel secret
}
```

### 12.4 L1 / L2 触发语义
| 状态 | 含义 | 手段 | 动作 |
|---|---|---|---|
| **L1 = `runner_down`** | Runner 故障 | 重建 Runner，atlas1 同时部署 A+B | ① `POST /api/v1/admin/trigger {"count":1}` ② 轮询 `GET /api/v1/admin/active` 等 agent online ③ `POST /api/v1/admin/exec` 下发 `ctl.py template apply atlas1 --agent <id>`（atlas1 Step4 装 Tunnel A + Step6 装 Tunnel B） |
| **L2 = `cf_ws_blocked`** | Tunnel A WS 故障，Tunnel B 正常 | az.yml Host A→B | ① 检测 Tunnel B WS（curl Host B → 期望 101）② rewrite az.yml: ws-opts.headers.Host 全部改为 Host B ③ push GitHub ④ verify。**不重建 Runner、不改 atlas1 secret、不改 server** |
| `host_blocked` (L3) | GFW 封域名 | 换 Host（cf_ip_rotate.py 主链路，§4） | — |
| `healthy` | 全通 | — | 退出 |

### 12.5 操作命令（oracle-agent 服务器，`/opt/oracle-agent`）
```bash
# 必须以包方式运行
cd /opt/oracle-agent
sudo venv/bin/python -m server.ctl template list
sudo venv/bin/python -m server.ctl template show atlas1    # 或 atlas2
sudo venv/bin/python -m server.ctl template set-secret atlas1 cf_tunnel_a <TOKEN>  # atlas2 同理
sudo venv/bin/python -m server.ctl template import atlas1 /tmp/atlas1.yaml  # atlas2 同理
sudo venv/bin/python -m server.ctl template apply atlas1 --agent <agent_id>  # 或 atlas2
```
- Admin API：`GET /api/v1/admin/agents`、`GET /api/v1/admin/tasks`、`POST /api/v1/admin/trigger`、`GET /api/v1/admin/active`、`POST /api/v1/admin/exec`（需 `Authorization: Bearer ORACLE_ADMIN_TOKEN`）。

### 12.6 敏感信息与待办
- ⚠️ token 在 `oracle.db` template_secrets 表，`template show atlas1` 不打印 secret 值（只显示 key 名）。

### 12.7 atlas2 模板 —— 第二控制面变体（2026-10-07 新增）

**设计原则：任务模板共用业务面隧道，独立控制面服务。**

atlas1 与 atlas2 两个模板的 6 步完全相同，唯一区别在 Step 4 拉取的控制面服务文件：

| 模板 | 控制面服务 | 业务面 Tunnel A | 备用面 Tunnel B | Secrets |
|------|-----------|----------------|----------------|---------|
| `atlas1` | `atlas1.service` | `cf_tunnel_a`（共用） | `cf_tunnel_b`（共用） | `cf_tunnel_a`, `cf_tunnel_b` |
| `atlas2` | `atlas2.service` | `cf_tunnel_a`（共用） | `cf_tunnel_b`（共用） | `cf_tunnel_a`, `cf_tunnel_b` |

- **共用业务面隧道**：两个模板引用同一组 `cf_tunnel_a` / `cf_tunnel_b` secret（同一 connector token），部署的 `cloudflared.service`（Tunnel A）和 `cloudflared-biz.service`（Tunnel B）完全一致。
- **独立控制面服务**：Step 4 分别拉 `atlas1.service` / `atlas2.service`，控制面（runner 管理/shell-mcp/tgbot）可以按需选择不同变体。
- **apply 选择**：L1 触发时可选 `template apply atlas1` 或 `template apply atlas2`，取决于需要哪个控制面服务。
- **secret 维护**：更新 connector token 时需同时更新两个模板（`set-secret atlas1 cf_tunnel_a <TOKEN>` + `set-secret atlas2 cf_tunnel_a <TOKEN>`），cf_tunnel_b 同理。

- [ ] **更新 `cf_tunnel_a` secret**：从旧 token 换为当前 tunnel connector token（见 §12.3）。
- [ ] **获取备用 tunnel connector token**（见 §12.9）。
- [ ] **live-verify**：trigger → apply atlas1 → 确认 cloudflared.service + cloudflared-biz.service + xray + shell-mcp + tgbot 全绿 + TG 通知。
- [ ] `cf_ip_rotate.py` 与 L1/L2 集成。

### 12.7 Bugfix：`POST /api/v1/admin/exec` agent_id 直连恒报 `agent_offline`（2026-09-25）
- **现象**：`exec`（`_resolve_exec_target` agent_id 分支）对在线 agent 恒报 `agent_offline`，但 `GET /active` 显示 online。
- **根因**：`agents` 表无 `online` 列；`db.get_agent()` 返回原始 row，`ag.get("online")` 恒为 `None`。
- **修复**：`server/api/admin.py` `_resolve_exec_target` 改为按 `last_seen >= now - ONLINE_AFTER_SEC`（180s）判在线。
- **验证**：`py_compile` OK → restart → exec agent_id 返回 `resolved_by: agent_id` + 任务 success。

### 12.8 GitHub 多账号冗余（2026-09-25，2026-10-07 更新）
- **目标**：主账号 PAT 失效时自动切备用账号，各账号在自有 `oracle-agent-bootstrap` 仓库触发同一套 bootstrap workflow。
- **当前配置**（`/opt/oracle-agent/server/data/accounts.json`）：
  - 主账号：`<REDACTED_USER>`
  - 备用账号：`<REDACTED_ACCOUNT>`
- **实现**：
  - `server/gh.py`：`get_accounts()`（env GH_PAT/GH_OWNER/GH_REPO 为主 + `accounts.json` 为备用）、`gh_request()`（按账号顺序 failover，401/403/404 自动切下一个）、`ensure_bootstrap_repo()` / `ensure_all_bootstrap_repos()`（启动时自动创建 bootstrap 仓库）。
  - `server/main.py`：`lifespan()` 中 `load_accounts()` 后调 `gh.ensure_all_bootstrap_repos()`。
  - `server/api/admin.py`：4 处 GH 调用点改用 gh 模块多账号 failover。
- **验证**：failover 端到端已验证（主 PAT 置无效 → 切备用 → runner 注册 → atlas1 apply success）。
- **L1 移交**：cf_ip_rotate.py 的 L1 remediation **已移除**（2026-10-09）。L1 检测保留（记日志），runner 重建由 Oracle Agent persistent loop 负责（带 template+nonce 正确 dispatch，支持 atlas1+atlas2）。

### 12.9 备用 CF 账户 tunnel connector token（L2 阻塞项）
- **问题**：备用 CF 账户 `<REDACTED_CF_ACCOUNT>` 的 tunnel `<REDACTED_TUNNEL_UUID>`（zone `<REDACTED_DOMAIN>`）创建时随机生成了 tunnel secret，但 **secret 未保存**，无法构造 connector token。
- **Tunnel connector token 格式**：`base64({"a":account_id, "t":tunnel_id, "s":tunnel_secret})`
- **解决方案（二选一）**：
  1. **删除重建**：`DELETE /accounts/{id}/cfd_tunnel/{tunnel_id}` → `POST /accounts/{id}/cfd_tunnel`（用已知 secret）→ 构造 connector token
  2. **CF API 获取**：检查是否有 `GET /accounts/{id}/cfd_tunnel/{tunnel_id}/token` 端点可直接返回 connector token
- **L2 remediation 在此 token 获取前无法端到端测试**，但 L3（主链路）和 L1（Oracle Agent failover）不受影响。

---

## 13. 运维操作指南：添加/更换隧道

### 13.0 配置分布总览

| 配置位置 | 内容 | 修改方式 |
|----------|------|----------|
| **`.env`** (`/home/ubuntu/antiblocking/.env`) | cf_ip_rotate.py 用的 CF token、zone ID、tunnel UUID（主+备）、GitHub PAT 路径、Oracle Agent API | 手动编辑文件 |
| **atlas1 template secrets** (oracle.db) | `cf_tunnel_a`（主业务隧道 connector token）、`cf_tunnel_b`（备用业务隧道 connector token） | `ctl.py template set-secret atlas1 <key> <value>` |
| **PLAN.md** | 文档记录（§2.4 账户表、§12.3 拓扑表） | 手动编辑 |

> **核心设计**：`.env` 是 cf_ip_rotate.py 的配置中心（CF API token + zone + tunnel UUID），atlas1 secret 是 runner 部署的配置中心（connector token）。L2 failover 只切 DNS CNAME，不动 atlas1 secret、不重部署 runner。B connector token 只存 atlas1 `cf_tunnel_b`，不存 .env——cf_ip_rotate.py 的 L2 动作只需检测 B WS + 改 az.yml Host，不需要 B connector token。

> **CHECK_HOST 检测来源**：cf_ip_rotate.py 的检测始终读 `.env` 中的 `CHECK_HOST`，**不从 GitHub az.yml 读**。az.yml 对 cf_ip_rotate.py 是**只写**——L2 remediation 会 push 新 Host 到 az.yml，但检测不用它。若 `.env` CHECK_HOST 与 GitHub az.yml 的 `ws-opts.headers.Host` 不一致（如手动改了 az.yml 没改 .env），检测以 `.env` 为准，可能与 runner 实际连接的隧道不匹配。

### 13.1 更换主业务隧道（Tunnel A）

**场景**：当前 Tunnel A 的 CF 账户/zone/隧道要换掉（token 过期、账户被封、迁移到新域名等）。

> **A/B 对等原则**：Tunnel A 和 Tunnel B 是对等关系，不是主从。CHECK_HOST 指向谁，谁就是当前活跃隧道。更换 A 的凭证不影响 B，也不需要改 CHECK_HOST——CHECK_HOST 只在 L2 failover 时才切。

**改动 4 处**：CF DNS + .env + DB template_secrets（atlas1 + atlas2）+ PLAN.md。

```bash
# ── Step 1: 在新 CF 账户创建 tunnel，拿到 connector token ──
#    Dashboard 或 API：
#    POST /accounts/{account_id}/cfd_tunnel
#    Body: {"name": "<REDACTED_TUNNEL>", "tunnel_secret": "<已知 secret>"}
#    → 记住返回的 tunnel_id
#    → 构造 connector token = base64({"a":account_id, "t":tunnel_id, "s":tunnel_secret})
#    或直接用 cloudflared service install <token> 得到的 token

# ── Step 2: 配置 tunnel ingress（隧道路由）──
#    在 CF Dashboard 或通过 API 添加 public hostname：
#    uwu.{新zone} → http://localhost:11451
#    PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations
#    Body: {"config": {"ingress": [
#      {"hostname": "uwu.{新zone}", "service": "http://localhost:11451"},
#      {"service": "http_status:404"}
#    ]}}
#    注意：手动在 Dashboard 添加 route 不会自动创建 DNS CNAME，需 Step 3 手动建。

# ── Step 3: 创建 DNS CNAME（CF 边缘 → tunnel）──
#    POST /zones/{zone_id}/dns_records
#    Body: {"type":"CNAME", "name":"uwu.{新zone}", "content":"{tunnel_id}.cfargotunnel.com", "proxied":true, "ttl":1}
#    验证：dig +short uwu.{新zone} 应返回 CF 边缘 IP（104.21.x / 172.67.x）

# ── Step 4: 更新 .env（cf_ip_rotate.py 检测和建 DNS 用这些值）──
#    编辑 /home/ubuntu/antiblocking/.env，改 Tunnel A 段的 5 行：
#    CLOUDFLARE_API_TOKEN=<新账户 API token，cfat_ 前缀>
#    CLOUDFLARE_ZONE_ID=<新 zone ID>
#    CLOUDFLARE_ZONE_NAME=<新 zone 域名>
#    CLOUDFLARE_ACCOUNT_ID=<新账户 ID>
#    CLOUDFLARE_TUNNEL_UUID=<新 tunnel UUID>
#    不要改 CHECK_HOST（A/B 对等，CHECK_HOST 只在 L2 failover 时切）

# ── Step 5: 更新 Oracle Agent DB template_secrets（atlas1 + atlas2 都要改）──
#    两个模板都引用 cf_tunnel_a，必须同时更新：
sudo python3 -c "
import sqlite3
conn = sqlite3.connect('/opt/oracle-agent/server/data/oracle.db')
conn.execute(\"UPDATE template_secrets SET value=? WHERE key='cf_tunnel_a'\", ('<NEW_CONNECTOR_TOKEN>',))
conn.commit()
print(f'Updated {conn.total_changes} rows')
conn.close()
"
#    验证：
sudo python3 -c "
import sqlite3
conn = sqlite3.connect('/opt/oracle-agent/server/data/oracle.db')
for row in conn.execute(\"SELECT id, template_id, key FROM template_secrets WHERE key='cf_tunnel_a'\"):
    print(row)
"

# ── Step 6: 更新 PLAN.md ──
#    替换旧 zone 名、account ID、zone ID、tunnel UUID 等引用：
#    sed -i 's|旧zone|新zone|g; s|旧account_id|新account_id|g; s|旧zone_id|新zone_id|g; s|旧tunnel_uuid|新tunnel_uuid|g' PLAN.md

# ── Step 7（可选）: 如果当前 az.yml 的 Host 指向旧 zone，需要切到新 zone ──
#    只有当 CHECK_HOST 在旧 zone 上时才需要：
#    手动改 nodes/ms/az.yml 里 19 个 proxy 的 ws-opts.headers.Host
#    或跑 cf_ip_rotate.py 的 L3 remediation 自动换
#    如果 CHECK_HOST 在另一个 tunnel（B）上，则不需要改 az.yml
```

**实测记录（2026-10-08，<REDACTED_DOMAIN> → <REDACTED_DOMAIN>）**：
- 旧 Tunnel A：account `a6bdd09b…`, zone `<REDACTED_DOMAIN>`, tunnel `<REDACTED>…`
- 新 Tunnel A：account `<REDACTED>…`, zone `<REDACTED_DOMAIN>`, tunnel `<REDACTED>…`
- 新隧道路由：`<REDACTED_HOST>` → `http://localhost:11451`
- DNS CNAME：`<REDACTED_HOST>` → `<REDACTED>-….cfargotunnel.com`（proxied）
- CHECK_HOST 未改（仍为 `<REDACTED_HOST>`，在 Tunnel B 上）
- template_secrets 更新 2 行（atlas1 id=2 + atlas2 id=9）
### 13.2 添加/更换备用业务隧道（L2 failover 目标）

**场景**：<REDACTED> 的 tunnel 废了要换，或想加第三个备用账户。

**改 .env + atlas1 cf_tunnel_b + PLAN.md**。.env 给 cf_ip_rotate.py 用（L2 切 DNS），cf_tunnel_b 给 runner 部署用（cloudflared-biz.service 常驻）。L2 failover 时只切 DNS CNAME，不动 atlas1 secret、不重部署 runner。

```bash
# ── Step 1: 在新 CF 账户创建 tunnel，拿到 connector token ──
#    POST /accounts/{account_id}/cfd_tunnel
#    → 构造 connector token = base64({"a":account_id, "t":tunnel_id, "s":tunnel_secret})

# ── Step 2: 在新账户的 zone 建 DNS + tunnel ingress（和主隧道一样的路由）──
#    DNS CNAME:
#    POST /zones/{zone_id}/dns_records
#    Body: {"type":"CNAME", "name":"opencode-api.新zone", "content":"{tunnel_id}.cfargotunnel.com", "proxied":true, "ttl":1}
#
#    Tunnel ingress:
#    PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations
#    Body: {"config": {"ingress": [
#      {"hostname":"opencode-api.新zone", "service":"http://localhost:11451"},
#      {"service":"http_status:404"}
#    ]}}

# ── Step 3: 更新 .env 备用配置段（cf_ip_rotate.py 用）──
#    编辑 /home/ubuntu/antiblocking/.env，改这几行：
#    CLOUDFLARE_API_TOKEN_BACKUP=<新备用账户 API token>
#    CLOUDFLARE_ACCOUNT_ID_BACKUP=<新账户 ID>
#    CLOUDFLARE_ZONE_ID_BACKUP=<新 zone ID>
#    CLOUDFLARE_ZONE_NAME_BACKUP=<新 zone 域名>
#    CLOUDFLARE_TUNNEL_UUID_BACKUP=<新 tunnel UUID>
#    （B connector token 不存 .env，只存 atlas1 cf_tunnel_b）

# ── Step 3b: 更新 atlas1 模板 secret（runner 部署 cloudflared-biz.service 时用）──
cd /opt/oracle-agent
sudo venv/bin/python -m server.ctl template set-secret atlas1 cf_tunnel_b <新备用 connector token>

# ── Step 4: 更新 PLAN.md §2.4 账户表和 §12.9 ──
```

### 13.3 （已合并入 §13.2）

§13.2 Step 3b 已覆盖 atlas1 `cf_tunnel_b` 的更新。添加/更换备用隧道只需走 §13.2 全流程（.env + atlas1 + PLAN.md）。


### 13.4 操作速查表

| 操作 | .env | atlas1 secret | az.yml | PLAN.md | 说明 |
|------|------|---------------|--------|---------|------|
| 换主隧道 (Tunnel A) | ✅ 主配置段 | ✅ `cf_tunnel_a` | ✅（zone 变了才改） | ✅ | runner + 检测脚本 + 客户端全链路更新 |
| 加/换备用隧道 (Tunnel B) | ✅ 备用配置段 | ✅ `cf_tunnel_b` | ❌ | ✅ | .env 给 cf_ip_rotate.py 用，cf_tunnel_b 给 runner 部署用，两者是同一隧道的两个配置面 |
| 换 GitHub PAT | ❌ | ❌ | ❌ | ✅ | 改 oracle-agent `accounts.json`，见 §12.8 |

### 13.5 自动化添加备用隧道（`--add-backup` 子命令）

**动机**：§13.2 的手动流程需要 4 步（建 tunnel → 建 DNS+ingress → 改 .env + atlas1 → 更新文档），其中前 3 步可以自动化。

**两种 token 的区别（重要，勿混淆）**：

| Token | 用途 | 给谁 | 格式 |
|-------|------|------|------|
| **CF API token** (`cfat_xxx`) | 管 CF 资源：建 DNS、加 ingress、创建 tunnel | cf_ip_rotate.py | `cfat_` 前缀 |
| **Tunnel connector token** (`eyJhIjoi...`) | runner 上 cloudflared 建立隧道连接 | atlas1 `cf_tunnel_b` secret（不存 .env） | `base64({"a","t","s"})` |

**两种模式**：

| 模式 | 给什么 | 前提 | 脚本做什么 |
|------|--------|------|-----------|
| **Mode 1: 全自动** | `--token` (API token) + `--account-id` + `--zone-id` + `--zone-name` | zone 已托管 CF | 建 tunnel → 建 DNS → 加 ingress → 写 .env + atlas1 |
| **Mode 2: 半自动** | `--connector-token` (eyJ...) + `--zone-name` | 用户已在 CF Dashboard 手动建好 tunnel + DNS + ingress | 只写 .env + atlas1（跳过所有 CF API 调用） |

> Mode 1 适合从零开始：给个 API token，脚本全包。
> Mode 2 适合已手动配好隧道：你在 CF Dashboard 建了 tunnel、加了 public hostname、配了 DNS CNAME，只想让 antiblocking 知道它的存在。

**用法 — Mode 1（全自动）**：

```bash
python3 /home/ubuntu/antiblocking/cf_ip_rotate.py --add-backup \
  --token cfat_xxx \
  --account-id <REDACTED_ACCOUNT_ID> \
  --zone-id <REDACTED_ZONE_ID> \
  --zone-name <REDACTED_DOMAIN>
```

**用法 — Mode 2（半自动）**：

```bash
python3 /home/ubuntu/antiblocking/cf_ip_rotate.py --add-backup \
  --connector-token eyJhIjoi... \
  --zone-name <REDACTED_DOMAIN>
```

**Mode 1 流程（全自动，8 步）**：

```
--add-backup --token T --account-id A --zone-id Z --zone-name N
      │
      ├─ 1. 验证 token 权限（GET /accounts/{A} + GET /zones/{Z}/dns_records）
      │     权限不足 → 报错退出，提示需要 DNS:Write + Cloudflare One Connectors:Write
      │
      ├─ 2. 创建 tunnel（POST /accounts/{A}/cfd_tunnel）
      │     生成随机 tunnel_secret（32 bytes base64），记住它
      │     → 拿到 tunnel_id
      │
      ├─ 3. 构造 connector token
      │     base64({"a": A, "t": tunnel_id, "s": tunnel_secret})
      │
      ├─ 4. 建 DNS CNAME
      │     POST /zones/{Z}/dns_records
      │     {type: "CNAME", name: "opencode-api.{N}", content: "{tunnel_id}.cfargotunnel.com", proxied: true, ttl: 1}
      │
      ├─ 5. 加 tunnel ingress
      │     PUT /accounts/{A}/cfd_tunnel/{tunnel_id}/configurations
      │     {config: {ingress: [
      │       {hostname: "opencode-api.{N}", service: "http://localhost:11451"},
      │       {service: "http_status:404"}
      │     ]}}
      │
      ├─ 6~8. 共享步骤（见下方）
```

**Mode 2 流程（半自动，跳过 step 1-5）**：

```
--add-backup --connector-token CT --zone-name N
      │
      ├─ 1. 解析 connector token
      │     decode base64(CT) → {"a": account_id, "t": tunnel_id, "s": secret}
      │     从 tunnel_id 和 zone_name 构造 .env 所需字段
      │
      ├─ 6~8. 共享步骤（见下方）
```

**共享步骤（Mode 1 和 Mode 2 都执行）**：

```
      ├─ 6. 写进 .env 备用配置段
      │     CLOUDFLARE_API_TOKEN_BACKUP=T          (Mode 1) / 留空       (Mode 2)
      │     CLOUDFLARE_ACCOUNT_ID_BACKUP=A         (Mode 1) / 从 CT 解析  (Mode 2)
      │     CLOUDFLARE_ZONE_ID_BACKUP=Z            (Mode 1) / 留空       (Mode 2)
      │     CLOUDFLARE_ZONE_NAME_BACKUP=N
      │     CLOUDFLARE_TUNNEL_UUID_BACKUP=tunnel_id
      │     （connector_token 只写 atlas1 cf_tunnel_b，不写 .env）
      │
      ├─ 6b. 更新 atlas1 模板 secret
      │     调 Oracle Agent API: set-secret atlas1 cf_tunnel_b = connector_token
      │     （runner 下次部署时 cloudflared-biz.service 用这个 token 连接）
      │
      ├─ 7. 预探验证
      │     sleep 5 → curl http://opencode-api.{N}/ → 期望 400（非 404）
      │     400 = DNS + ingress 都配对了 ✅
      │     404 = ingress 没生效，报警
      │
      └─ 8. 输出确认
            "备用隧道添加成功: opencode-api.{N} → tunnel {tunnel_id}"
            "L2 failover 已就绪，cf_ip_rotate.py 检测到 cf_ws_blocked 时自动切换"
```

**--add-backup 也可用于更换现有备用隧道**：如果 .env 已有备用配置，`--add-backup` 会先删除旧 tunnel（Mode 1: DELETE /accounts/{old}/cfd_tunnel/{old_id}），再创建新的。加 `--keep-old` 可保留旧 tunnel（仅切换 .env 指向）。

**前置条件**：
- **Mode 1**: API token 权限 DNS:Write + Cloudflare One Connectors:Write；zone 已在 CF 托管
- **Mode 2**: 用户已手动在 CF Dashboard 建好 tunnel + DNS CNAME + tunnel ingress（public hostname → localhost:11451）
- **共用**: .env 已存在（脚本追加备用配置段，不覆盖主配置）

**与手动流程的关系**：`--add-backup` Mode 1 = §13.2 Step 1-3 的自动化；Mode 2 = §13.2 Step 3-3b 的自动化（跳过 Step 1-2）。Step 4（更新 PLAN.md）仍需手动——文档变更不适合自动化。



