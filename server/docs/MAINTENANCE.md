# Oracle Agent Server — 维护手册 (Production v0.2.0)

> 适用对象：生产控制端 `/opt/oracle-agent`（`oracle-agent.service`）。
> 本文档描述 **当前生产代码** 的真实结构与运维操作。

## 0. 拓扑与服务

```
GitHub Actions Runner（临时 VM，sleep 99999 常驻）
   │  outbound HTTPS pull（每 30s），永不开放公网入站
   ▼
Cloudflare Tunnel → oracle-agent.femboy.us.ci → 127.0.0.1:8700
   ▼
oracle-agent.service（FastAPI + SQLite，/opt/oracle-agent）
```

- 服务：`systemd` unit `oracle-agent`；WorkDir `/opt/oracle-agent`；端口 **8700**
- 前端：无 UI；管理走 `/api/v1/admin/*`（Bearer admin token）或 `ctl.py` CLI
- 数据：`server/data/oracle.db`（SQLite）；日志 `server/data/access.log`（rolling）
- Python：`/opt/oracle-agent/venv/`（system python3.12 + fastapi/uvicorn/pyyaml）

## 1. 模块结构（当前生产）

```
/opt/oracle-agent/
├── venv/                        Python 虚拟环境（勿删）
└── server/
    ├── main.py                  FastAPI 入口 + 访问日志(脱敏)中间件 + supervisor 启动
    ├── config.py                ACCESS_LOG_* 等路径常量
    ├── db.py  ★                 SQLite 持久化（agents/tasks/runs/templates/secrets + 生命周期迁移 + cancel_intent）
    ├── models.py / auth.py      Pydantic 模型 / Bearer 校验
    ├── ctl.py                   管理 CLI（agentctl：list/run/task/template）
    ├── supervisor.py            Supervisor(Phase 4A)：已停用（enabled: false）
    ├── template.py              Task 模板引擎（YAML、{secret: REF}、步骤序列执行）
    ├── gh.py  ★                 GitHub 多账号 failover + 状态机查询函数
    ├── persistent.py  ★         GitHub-side 状态机：reconcile + drift 检测 + nonce dispatch + 1:1 映射 + PRUNE
    └── api/
        ├── agents.py            register / heartbeat / report（agent 侧）
        ├── tasks.py             pull（长轮询）/ result
        └── admin.py             管理 API（见 §4）
```

> `★` = v0.2.0 重写/大幅修改的模块

### gh.py 新增函数（v0.2.0）

| 函数 | 作用 |
|------|------|
| `gh_list_active_runs(per_page=30)` | 拉取 GitHub 所有活跃 run（queued + in_progress），一次请求 |
| `gh_get_run(run_id)` | 查单个 run 的最终状态和 conclusion |
| `gh_find_run_by_nonce(nonce, template, per_page=10)` | 按 display_title 中的 nonce 精确匹配认领 run |
| `gh_dispatch_workflow(template, nonce)` | 带 nonce + template 输入 dispatch workflow |
| `gh_cancel_run(run_id)` | 取消指定 run |

### 配置文件（默认路径，均可用 env 覆盖）

| 文件 | 消费方 | 作用 |
|---|---|---|
| `/etc/default/oracle-agent` | systemd EnvironmentFile | 主配置（token/主 GH 账号/轮换参数） |
| `server/data/accounts.json` | `gh.py`（admin failover） | 备用 GitHub 账号 |
| `/opt/oracle-agent/accounts.json` | `supervisor.py` | 自动排程 profiles（缺失则退化为单账号 env） |
| `/opt/oracle-agent/supervisors.json` | `supervisor.py` | auto-supervisor 配置（已全部 `enabled: false`） |

## 2. 环境变量（/etc/default/oracle-agent）

| 键 | 说明 |
|---|---|
| `ORACLE_ADMIN_TOKEN` | admin API Bearer token（所有 `/api/v1/admin/*` 必填） |
| `ADMIN_AUTH_ENABLED` | `1` 生效（否则仅 dev） |
| `LOG_VIEW_TOKEN` | 日志/模板只读查看 token |
| `GH_OWNER` / `GH_REPO` | 主 GitHub 账号与仓库（默认 `oracle-agent-bootstrap`） |
| `GH_PAT` | 主 GitHub PAT（`GITHUB_TOKEN` 亦可） |
| `ORACLE_ACCOUNTS` / `ORACLE_ACCOUNTS_CONFIG` / `SUPERVISORS_CONFIG` | 可选路径覆盖 |
| `PERSISTENT_TEMPLATES` | 逗号分隔的模板名（如 `atlas1,atlas2`），空=禁用轮换 |
| `ROTATION_AGE_MIN` | run 活到多少分钟时触发轮换（默认 `355`，留 5min 余量） |
| `ROTATION_CHECK_INTERVAL` | 检查间隔秒（默认 `300`） |
| `AGENT_ONLINE_TIMEOUT` | 等新 agent 上线超时秒（默认 `600`） |
| `PRUNE_DRY_RUN` | `true`=只打日志不实际 cancel（默认 `true`，**已于 2026-10-09 改为 `false`**） |
| `STALE_RUN_MAX_AGE_HOURS` | 僵尸 run 回收阈值小时（默认 `6.5`） |

> ⚠️ 权限：`/etc/default/oracle-agent` 保持 root 600；服务以 `oracle-agent` 用户运行。

## 3. Agent 协议 API（/api/v1/agent，Runner 调用）

Runner（GitHub Actions bootstrap runner）通过以下端点与 Server 通信。认证：Bearer token（注册时分配，后续请求需 `Authorization: Bearer <token>`）。

| 方法/路径 | 用都 |
|---|---|
| `POST /register` | Runner 注册；Server 分配 agent_id + token，或验证已有 agent 的 token 后重新注册 |
| `POST /heartbeat` | 心跳上报（status: starting/online/degraded），返回 heartbeat_interval_sec |
| `POST /report` | 事件上报（event + data），存入 agent meta events 列表（保留最近 20 条） |
| `POST /pull` | 长轮询拉取待执行任务（timeout_sec，每 1s 轮询 DB，最多等 PULL_TIMEOUT_MAX 秒） |
| `POST /result` | 提交任务结果（status: success/failed/timeout/rejected），重复提交返回 409 |

系统端点（main.py 直接定义，无需 admin token）：

| 方法/路径 | 用都 |
|---|---|
| `GET /healthz` | 健康检查（无需认证） |
| `GET /api/v1/admin/supervisors-status` | Supervisor 状态（内部/agentctl 用，不进 Swagger） |

## 4. 管理 API（/api/v1/admin，需 Bearer）

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 存活（depends admin 校验） |
| `GET /agents` | agent 列表（含在线状态 last_seen） |
| `GET /tasks[?agent=&status=]` | 任务列表 |
| `GET /runs` / `/runs/{run_id}` / `/runs/by-ip/{ip}` | GitHub run 档案 |
| `GET /active` | 在线 agents + 进行中 runs |
| `GET /github/workflows?status=` | GitHub 侧 runs（多账号 failover） |
| `POST /trigger {count}` | dispatch bootstrap workflow（多账号，0=仅校验） |
| `POST /exec` / `/exec-many` | 给 agent 下发命令（one / many / all_online） |
| `GET /tasks/{task_id}` | 单任务详情 |
| `GET /logs/{view_token}` | 访问日志（token 鉴权，脱敏） |
| `GET /template-view/{view_token}` | 模板只读视图 |
| `POST /cancel {run_id\|id\|vm_id\|run_ids\|all_in_progress}` | 取消 GH run（多账号） |

## 5. GitHub 多账号 failover

### 4.1 配置

- 主账号：env `GH_PAT`+`GH_OWNER`+`GH_REPO`（`/etc/default/oracle-agent`）
- 备用账号：`server/data/accounts.json` 的 `profiles`（`name/owner/repo/pat`）
- 要求：**每个账号各有自己独立的 `oracle-agent-bootstrap` 仓库**（非 fork）
- bootstrap.yml 已更新：含 `nonce` + `template` 输入和 `run-name: "${{ inputs.template }}-${{ inputs.nonce }}"`

### 4.2 两条代码路径

| 路径 | 代码 | 用途 | 状态管理 |
|------|------|------|----------|
| **gh.py** | `gh_request()` 遍历所有账号，401/403/404/422 → continue 下一账号 | persistent.py（atlas1/atlas2 每 60s 轮询）、admin API | 无状态：每次请求都从第一个账号开始试 |
| **supervisor.py** | `Profile` 状态机（healthy/cooldown/unhealthy），`_pick_profile()` 跳过 unavailable | 自动排程（当前未启用） | 有状态：unhealthy 永久标记，直到重启 |

### 4.3 Failover 覆盖范围

**会自动 failover 的场景**（gh.py 遇到 401/403 → 自动切到下一个 PAT）：

| 场景 | HTTP 状态码 | gh.py 行为 | supervisor.py 行为 |
|------|------------|-----------|-------------------|
| PAT 失效/过期 | 401 | ✅ fallthrough 到备用账号 | ✅ mark unhealthy，后续跳过 |
| 账户被 suspended | 403 | ✅ fallthrough 到备用账号 | ✅ mark unhealthy，后续跳过 |
| 账户被限制使用 Actions | 403 | ✅ fallthrough 到备用账号 | ✅ mark unhealthy，后续跳过 |
| 仓库不存在/无权限 | 404 | ✅ fallthrough 到备用账号 | ✅ mark unhealthy，后续跳过 |

**不会自动 failover 的场景**：

| 场景 | HTTP 状态码 | gh.py 行为 | 说明 |
|------|------------|-----------|------|
| API rate limit | 429 | ❌ raise GHRequestError | 429 不在 fallthrough 列表中，直接失败；下个周期重试同一账号 |
| 私有分钟用完 | 403 (workflow_dispatch) | ⚠️ 部分 | dispatch 时 403 会 fallthrough，但即使切到备用账号，如果备用账号也用完了同样失败 |
| GitHub 5xx | 500/502/503 | ❌ raise | 临时故障，不切账号，下个周期重试 |
| 网络超时 | — | ❌ continue（但不是切账号，是跳过当前账号） | 网络问题，非账号问题 |
| 两个 PAT 都失效 | — | ❌ GHRequestError | 所有账号都失败，persistent.py 记日志后等下个周期 |

### 4.4 当前账号配置

```
主账号:  woluren  / oracle-agent-bootstrap  (env GH_PAT)
备用账号: sirkur   / oracle-agent-bootstrap  (accounts.json)
```

### 4.5 注意事项

- **gh.py 无状态**：每次 API 调用都从主账号开始试。如果主账号持续 401，每 60s 轮询都会先失败一次再 fallthrough，增加约 1-2s 延迟。功能正常，但不高效。
- **supervisor.py 不可恢复**：`mark_unhealthy` 后没有定期健康检查，profile 一旦 unhealthy 就永久跳过（直到 `systemctl restart oracle-agent`）。
- **supervisor.py 启动时缓存**：`load_accounts()` 在 `lifespan()` 里只调一次。更新 PAT 后必须重启服务。
- **429 不会 failover**：如果主账号被 API rate limit（非 suspended，只是调用太频繁），gh.py 不会切到备用账号，而是直接失败等下个周期。这是设计选择：rate limit 是临时的，切账号会导致两个账号都被 limit。
- **更新 PAT 步骤**：① 编辑 `accounts.json` 或 `/etc/default/oracle-agent` → ② `systemctl restart oracle-agent`


## 6. CLI（agentctl / ctl.py）

```bash
sudo -u oracle-agent /opt/oracle-agent/venv/bin/python -m server.ctl <cmd>
```

| 命令 | 作用 |
|---|---|
| `list` | agents + tasks 一览 |
| `run list` / `run show <id>` / `run by-ip <ip>` | run 档案 |
| `task list` / `task status <id>` / `task push` | 任务操作 |
| `template import <file.yaml>` / `list` / `show` / `set-secret` / `apply <name> <agent>` / `runs` | 模板生命期 |

## 7. 模板引擎

- YAML 定义步骤：`name` + `run`（argv）+ `timeout_sec` + `env:{KEY:{secret: REF}}`
- secrets 存 DB，不落模板文件；引用缺失在 apply 前一次性校验报错
- `template apply`：串行执行到第一个失败，记 `template_runs` 审计表

## 8. 日常运维

### 7.1 重启 / 状态

```bash
sudo systemctl restart oracle-agent
systemctl status oracle-agent
journalctl -u oracle-agent -n 100 -f
```

### 7.2 健康检查

```bash
curl -s http://127.0.0.1:8700/healthz
TOKEN=$(grep ORACLE_ADMIN_TOKEN /etc/default/oracle-agent | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8700/api/v1/admin/active
```

### 7.3 查看 Persistent Rotation 日志

```bash
# 全部轮换日志
sudo journalctl -u oracle-agent --no-pager | grep persistent

# 最近 5 分钟，含 drift 和 PRUNE
sudo journalctl -u oracle-agent --since '5 min ago' --no-pager -o cat | grep -E 'persistent|drift|PRUNE'
```

典型输出：
```
INFO [oracle.persistent] persistent_runner_loop started: templates=['atlas1', 'atlas2'], prune_dry_run=False
INFO [oracle.persistent] persistent: atlas1 OK — run 37737848303 (status=in_progress, agent=...bbf30434)
INFO [oracle.persistent] persistent: atlas2 OK — run 37737848313 (status=in_progress, agent=...984f0578)
INFO [oracle.persistent] persistent: drift fix — run 37731876820 completed on GitHub (conclusion=cancelled), updated local DB
```

### 7.4 触发一次 bootstrap runner

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"count":1}' \
  http://127.0.0.1:8700/api/v1/admin/trigger
```

### 7.5 给在线 agent 下发命令

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":"<id>","command":["uname","-a"]}' \
  http://127.0.0.1:8700/api/v1/admin/exec
```

### 7.6 取消 run / 释放 VM

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"run_id":<github_run_id>}' http://127.0.0.1:8700/api/v1/admin/cancel
```

### 7.7 备份 / 恢复

- DB 备份：`sqlite3 server/data/oracle.db ".backup /tmp/oracle.db.$(date +%F)"`（WAL 无需停机）
- 恢复：停服务，替换 DB，重启；`PRAGMA integrity_check` 应返回 `ok`

## 9. 源码备份（GitHub）

- 目标仓库：**`pornnewbee/oracle-agent`（private）**，分支 `main`
- 备份范围：`server/**`（不含 `data/oracle.db`、`access.log`、`accounts.json`）、`docs/**`
- 凭据不落库：所有 token 不外推；`server/data/accounts.json` 不入库

## 10. 故障排查速查

| 现象 | 排查 |
|---|---|
| `POST /admin/exec` 报 `agent_offline` | 按 `last_seen >= now-180s` 判在线 |
| trigger 404 | 对该 repo 做一次空 commit push 激活 workflow |
| dispatch 批准了但无 run | 检查 workflow 与 Actions enabled |
| run 一直 in_progress 不结束 | VM keep-alive（sleep 99999）按设计保留 |
| cancel 返回 409 | run 已 completed/cancelled（预期） |
| 无 `persistent:` 日志 | 检查 `PERSISTENT_TEMPLATES` 是否设置 |
| `drift fix` 频繁出现 | GitHub 侧 run 频繁完成/取消，检查外部干扰 |
| `PRUNE would cancel` | dry-run 下发现的冗余 run，确认后改 `PRUNE_DRY_RUN=false` |

## 11. 演进备注

- 待办：DB 明文 token → `{secret:REF}` 化；`cf_ip_rotate.py` 接入 L2 token 轮换
- 待办：supervisor 自动排程接通 accounts.json Profile 多账号

## 12. Persistent Runner Rotation — GitHub-side 状态机（v0.2.0）

### 11.1 设计原则

> **铁律：一个 run 只能拉取一个任务模板；一个任务模板同一时刻只能对应一个 run。**
> 即 **1 run ↔ 1 template 严格一对一**：N 个模板 → 同时维持 N 个 run、N 个 agent。

状态机以 **GitHub 侧为权威**，本地 DB 为缓存：

| 决策 | 权威源 | 辅助源（心跳） |
|---|---|---|
| run 是否还活着 | **GitHub** | ❌ 不参与 |
| 是否触发新 run | **GitHub**（本模板名下无活跃 run） | ❌ 绝不参与 |
| 是否取消多余 run | **GitHub** | ❌ 不参与 |
| run 能否下发任务 | 心跳 | GitHub 仅证明"活着" |
| 是否提前轮换 | GitHub run 年龄 | ✅ 心跳失联 → 提前换 |

### 11.2 工作流程

```
每 300s（ROTATION_CHECK_INTERVAL）:
  1. reap_stale_runs()          — 回收 >6.5h 仍非终态的僵尸记录
  2. _prune_orphan_runs()       — 清理无模板归属的冗余 run（dry-run 只打日志）
  3. for template in [atlas1, atlas2]:   # 并发，互不阻塞
       a. _reconcile_with_github(template):
          - 拉取 GitHub 所有活跃 run（一次请求，queued + in_progress）
          - 对每个 GitHub run，同步本地 DB 状态
          - Drift 检测：本地标 running 但不在 GitHub 活跃列表的 run
            → 查 GitHub 最终状态 → 更新本地 DB
          - 按 display_title（template-nonce 格式）+ 本地 DB 归属
            过滤出"属于本模板的 run"
       b. 决策：
          - 活跃 run 数 == 0 → TRIGGER（带 nonce dispatch）
          - 活跃 run 数 > 1 → PRUNE（保留最新，cancel 其余）
          - 活跃 run 数 == 1：
            * age >= 355min → RENEW（先起新 → 等上线 → 再退旧）
            * agent 离线 → RENEW_EARLY（先 cancel 旧 → 再起新）
            * 否则 → WAIT
```

### 11.3 Nonce-based 精确认领

dispatch 时传唯一 `nonce`（8 字符 hex），GitHub workflow 用 `run-name` 带出：

```yaml
# .github/workflows/bootstrap.yml（两个 GitHub 账号均已更新）
run-name: "${{ inputs.template }}-${{ inputs.nonce }}"
on:
  workflow_dispatch:
    inputs:
      nonce:    { description: "unique id", required: false, type: string }
      template: { description: "template name (1 run per template)", required: false, type: string }
```

dispatch 后按 `display_title` 中的 nonce 匹配认领（`gh_find_run_by_nonce`），杜绝抢错 run。
认领失败时 provisioning slot 保留，防止下轮重复触发，**不退化为"抓最新一条"**。

### 11.4 Drift 检测

本地 DB 会漂移（服务重启、dispatch 后 run_id 抓取失败、进程假死）。
每轮 reconcile 时，对本地标 `running` 但不在 GitHub 活跃列表的 run，主动查 GitHub 最终状态：

```python
# persistent.py _reconcile_with_github() 内
gh_active_ids = {gr.get("id") for gr in all_active}
for lr in local_active_all:
    rid = lr.get("run_id")
    if rid and rid not in gh_active_ids:
        final = gh_mod.gh_get_run(rid)      # 查 GitHub 最终状态
        if final and final.get("status") == "completed":
            db.reconcile_run_status(rid, "completed", final.get("conclusion"), now)
            log.info("persistent: drift fix — run %d completed on GitHub ...", rid)
```

### 11.5 PRUNE（收敛多余 run）

当同一模板名下出现多个活跃 run 时，保留最新的一个，cancel 其余。
无模板归属的 orphan run 也在此清理。

**已启用真实取消**（`PRUNE_DRY_RUN=false`，2026-10-09 起生效）：发现多余 run 时直接调用 GitHub cancel，不再仅打日志。
此前 dry-run 模式下曾因 DNS 失败导致重复触发 3 个 run/template，prune 只打日志未取消，修复 `gh_list_active_runs` 返回 `None` 后同时启用真实取消，系统已收敛至 1 run/template。

### 11.6 配置（环境变量）

`/etc/default/oracle-agent`:

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PERSISTENT_TEMPLATES` | （空=禁用） | 逗号分隔的模板名，如 `atlas1,atlas2` |
| `ROTATION_AGE_MIN` | `355` | run 活到多少分钟时触发轮换（5h55min，留 5min 余量） |
| `ROTATION_CHECK_INTERVAL` | `300` | 检查间隔（秒） |
| `AGENT_ONLINE_TIMEOUT` | `600` | 等新 agent 上线超时（秒，10min） |
| `PRUNE_DRY_RUN` | `false` | 已启用真实取消（2026-10-09） |
| `STALE_RUN_MAX_AGE_HOURS` | `6.5` | 僵尸 run 回收阈值（GitHub 6h 硬限 + 0.5h 缓冲） |

### 11.7 模块结构

- `server/persistent.py` — GitHub-side 状态机（reconcile + drift + nonce dispatch + PRUNE）
- `server/gh.py` — 5 个状态机查询函数（§1 表）
- `server/db.py` — 生命周期列迁移 + `cancel_intent` + `reconcile_run_status` + `reap_stale_runs`
- `server/main.py` — `lifespan()` 中启动 `persistent_runner_loop`
- 日志前缀：`persistent:`，通过 `journalctl -u oracle-agent | grep persistent` 查看

### 11.8 幂等保护

- `_rotating` set 防止同一模板并发轮换
- 每个模板独立 `asyncio.create_task(asyncio.to_thread(...))`，互不阻塞
- trigger 失败不重试，等下个周期自动重试
- provisioning slot（`status='provisioning'`）占坑防重复触发，即使 run_id 抓取失败

### 11.9 故障排查

| 症状 | 排查 |
|------|------|
| 无 `persistent:` 日志 | 检查 `PERSISTENT_TEMPLATES` 是否设置 |
| `trigger attempt failed: HTTP 401` | GitHub PAT 过期，检查 `accounts.json` 或 `GH_PAT` |
| `no agent online for run X within 600s` | Runner 未注册或未上线，检查 GitHub Actions run 状态 |
| `template apply failed` | Agent 在线但模板执行失败，检查 `template_runs` 表和 agent 日志 |
| `drift fix` 大量出现 | GitHub 侧 run 频繁结束，检查是否有外部 cancel 或超时 |
| `PRUNE would cancel` | dry-run 发现冗余 run，确认后改 `PRUNE_DRY_RUN=false` |

## 13. 已知问题与演进历史

### 12.0 v0.2.0 完成的修复（2026-10-08）

| # | 操作 | 结果 |
|---|---|---|
| 1 | `supervisors.json` 全部置 `enabled: false` | Supervisor 停用，`never awaited` 警告归零 |
| 2 | 清理 `runs` 表 84 条僵尸记录 → `provisioning_timeout` | 解除 Persistent Rotation 阻塞 |
| 3 | 新增 `db.reap_stale_runs()` 超时看门狗 | >6.5h 仍非终态的记录强制判终 |
| 4 | **bootstrap.yml 加 nonce + template 输入 + run-name** | 两个 GitHub 账号均已更新 |
| 5 | **gh.py 新增 5 个状态机查询函数** | `gh_list_active_runs` / `gh_get_run` / `gh_find_run_by_nonce` / `gh_dispatch_workflow` / `gh_cancel_run` |
| 6 | **persistent.py 完整重写为 GitHub-side 状态机** | reconcile + drift 检测 + nonce dispatch + 1:1 映射 + PRUNE |
| 7 | **db.py 新增 cancel_intent 列 + 6 个生命周期函数** | `reconcile_run_status` / `get_active_runs_by_template` / `insert_provisioning` / `update_run_id` / `mark_cancel_intent` / `reap_stale_runs` |
| 8 | **修复 1:1 违规**：nonce-based 精确认领替代 `per_page=1` 抢最新 | 每个模板各自持有独立 run |
| 9 | **Drift 检测**：本地标 running 但 GitHub 已结束的 run 自动同步 | 消除 DB 状态漂移 |
| 10 | 清理 20 条 orphan run（template=None, origin=None） | DB active runs 从 22 降至 2 |

### 12.1 Supervisor 已停用（死代码）

`supervisors.json` 全部 `enabled: false`。两处 bug 不修：
1. `supervisor.py:313` — `asyncio.to_thread` 对 `async def` 无效，协程从未 await
2. `supervisors.json` — `"repo"` 缺 owner 前缀

结论：Supervisor 属死代码，atlas1/atlas2 由 Persistent Rotation（§12）维持。

### 12.2 已修复：双表分裂

v0.1.0 存在两套机制（Supervisor / Persistent Rotation）各自维护"正在跑"的记录，互不可见。
v0.2.0 停用 Supervisor，仅保留 Persistent Rotation，单一权威。

### 12.3 已修复：去重不覆盖 GitHub queued 状态

v0.1.0 仅查 `status=in_progress`，遗漏 `queued`。
v0.2.0 的 `gh_list_active_runs()` 一次请求拉取 `queued + in_progress`，queued 即占用名额。

### 12.4 已修复：违反一对一（两个模板挤在同一个 run 上）

v0.1.0 的 `_trigger_new_run` 用 `/actions/runs?per_page=1` 抓"最新一条"，两个模板并发 dispatch 时抢到同一个 run。

v0.2.0 修复：dispatch 时传 nonce，workflow 用 `run-name` 带出，按 `display_title` 匹配认领。
认领失败不退化为"抓最新一条"，provisioning slot 保留防重复触发。

实测修复后：
```
atlas1 → run 37737848303 (display_title=atlas1-0c81de2d)  ✅
atlas2 → run 37737848313 (display_title=atlas2-862daa37)  ✅
```

### 12.5 已修复：run_id 缺失导致重复触发

v0.1.0 中 3/35 条 `template_runs` 记录 `run_id IS NULL` → 下轮判定"无 run" → 重复触发。
v0.2.0 的 `insert_provisioning()` 先占坑（run_id 允许 NULL），dispatch 后再补 run_id。
provisioning 状态占用名额，彻底堵死重复触发。

### 12.6 僵尸 run 阻塞事故复盘（2026-10-08）

**现象**：Persistent Rotation 自 `02:16Z` 起静默停摆约 3 小时。

**根因**：GitHub 账号更换后，历史 run 用新 PAT 查询返回 404，`runs` 表 84 条记录永久停在 `queued`/`running`。守卫每轮返回最新一条后直接 `return`，rotation 停摆。

**修复**：`db.reap_stale_runs(max_age_hours)` 在每轮开头调用，超 6.5h 仍非终态的记录强制标记 `provisioning_timeout`。阈值取 GitHub 6h 硬限 + 0.5h 缓冲，无需 GitHub 确认即可判终态。

**教训**：本地 DB 状态必须有超时兜底，否则任何一次对账失败都会把临时抖动变成永久死锁。

### 12.7 待办

- DB 明文 token → `{secret:REF}` 化
- `cf_ip_rotate.py` 接入 L2 token 轮换
- supervisor 自动排程接通 accounts.json Profile 多账号（当前 gh.py 已多账号，supervisor 尚未启用）
- PRUNE 已启用真实取消（`PRUNE_DRY_RUN=false`，2026-10-09）
