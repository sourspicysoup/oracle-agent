# Run 生命周期与去重设计

> ✅ **本文档描述的设计已于 2026-10-08 实现并上线（v0.2.0）。**
> 当前生产代码以 [MAINTENANCE.md](./MAINTENANCE.md) §11 为准。
> 本文档保留作为设计原理参考。

## 1. 设计目标

### 铁律：run 与任务模板严格一对一

> **一个 run 只能拉取一个任务模板；一个任务模板同一时刻只能对应一个 run。**

| 推论 | 含义 |
|---|---|
| run 数 == 模板数 | N 个模板 → 同时存在 N 个 run → N 个 agent |
| 禁止"一 run 多模板" | v0.1.0 违反此条，v0.2.0 已修复 |
| 去重键 = `template` | 每个模板各自维护自己的 run，不需要 `template_group` |
| 不存在"多余 run" | 每个 run 都应有明确归属；无归属的 run 才是该取消的 |

设计目标（均已实现）：

1. **每个模板各自持有恰好一个"活跃"的 GitHub Actions run**（1 run ↔ 1 template）。✅
2. 若某个模板名下出现多个未结束 run，收敛到最新一个，取消其余。✅ (PRUNE，已启用)
3. run 的生命周期状态以 **GitHub 侧为权威**，本地心跳作为**辅助**判断可用性。✅
4. 消除两套机制并存导致的状态分裂。✅ (Supervisor 停用)
5. 每个 run 必须有明确归属——dispatch 后能**精确认领**自己的 run。✅ (nonce-based)

## 2. 权威源与辅助源的职责边界

| 决策 | 权威源 | 辅助源（本地心跳） |
|---|---|---|
| 这个 run 是否还活着 | **GitHub** | ❌ 不参与 |
| **是否触发新 run** | **GitHub**（未结束 run 数 == 0） | ❌ **绝不参与** |
| 是否取消多余 run | **GitHub** | ❌ 不参与 |
| 这个 run 能否下发任务 | 心跳 | GitHub 仅证明"活着" |
| **是否提前轮换** | GitHub run 年龄（6h 硬限制） | ✅ 心跳失联 → 提前换 |

**一句话：触发看 GitHub，轮换看 GitHub + 心跳。**

### 为什么 GitHub 做权威

本地 DB 会漂移，GitHub 不会：
- 服务重启 → 内存状态丢失
- dispatch 后抓 `run_id` 失败 → DB 无记录
- 进程假死 → 心跳停了但 DB 仍标 `running`

### 为什么还必须保留心跳

GitHub 的 `status` 只有三态，不知道 runner 能不能干活：

| GitHub 说 | 实际可能是 |
|---|---|
| `in_progress` | agent 尚未注册（还在跑启动脚本） |
| `in_progress` | agent 假死，心跳早断 |
| `in_progress` | 一切正常 |

## 3. 状态映射

```
GitHub status / conclusion     →  本地状态       →  占用名额
────────────────────────────────────────────────────────────
queued                         →  provisioning   →  ✅ 占
in_progress                    →  running        →  ✅ 占
completed / success            →  success        →  ❌ 释放
completed / failure            →  failed         →  ❌ 释放
completed / cancelled          →  cancelled      →  ❌ 释放
completed / timed_out          →  timeout        →  ❌ 释放
```

**关键**：`cancelled` 是 **conclusion 不是 status**。
- 判断"未结束"必须用 `status != 'completed'`
- 判断结果才读 `conclusion`

## 4. 决策流程

每个模板独立跑一遍这个决策，且必须先按归属过滤出"属于我的 run"。

```python
def decide(template: str) -> Action:
    # 1. 权威源：拉 GitHub 事实（一次请求，覆盖 queued + in_progress）
    runs = gh_list_active_runs(per_page=30)

    if runs is None:                                      # 所有账号查询失败（DNS/网络）
        return WAIT                                       # 跳过本轮，不触发不清理

    # 2. 【1:1 关键】先按归属过滤，只保留属于本模板的 run
    mine   = [r for r in runs if owned_by(r, template)]  # 依赖 §5.4 的 nonce / run-name
    active = [r for r in mine if r["status"] != "completed"]

    # 3. 对账：以 GitHub 为准修正本地 DB + drift 检测
    reconcile_db(template, active)

    # 4. 决策（全部按"本模板名下"计数）
    if len(active) == 0:
        return TRIGGER                                   # 带 nonce dispatch

    if len(active) > 1:
        keep = max(active, key=created_at)
        return PRUNE(keep=keep, cancel=[r for r in active if r is not keep])

    run = active[0]

    if age(run) >= RENEW_AFTER:                          # 接近 GitHub 6h 硬限制
        return RENEW                                     # 先起新 → 等上线 → 再退旧

    if heartbeat_stale(run, timeout=HEARTBEAT_STALE):    # agent 假死
        return RENEW_EARLY                               # 先 cancel 旧 → 再起新

    return WAIT
```

### RENEW 与 RENEW_EARLY 必须分开

| | RENEW | RENEW_EARLY |
|---|---|---|
| 触发条件 | run 年龄接近 6h | 心跳失联 |
| 旧 run 状态 | 仍可用 | 已废 |
| 顺序 | 先起新 → 等上线 → 再退旧 | 先 cancel 旧 → 再起新 |
| 是否中断 | 无缝 | 接受中断 |

## 5. 数据模型

### 5.1 去重键 = `template`（一对一）

✅ **已实现**：run 与模板严格 1:1，去重键就是 `template` 本身。

### 5.2 provisioning 状态（先占坑，后补 run_id）

✅ **已实现**：`db.insert_provisioning()` 先插记录（run_id 允许 NULL），dispatch 后再补。

```python
row_id = db.insert_provisioning(template, nonce, created_at=now)   # 先插，run_id NULL
rid = dispatch_and_find_run()                                      # 再抓
db.update_run_id(row_id, rid)                                      # 补上
```

### 5.3 cancel_intent 字段

✅ **已实现**：`runs` 表新增 `cancel_intent` 列。调用 GitHub cancel 后，run 不会立刻变 `completed`，
`cancel_intent` 标记意图，reconcile 时继续等待 GitHub 确认。

### 5.4 run_id 精确认领（nonce + run-name）

✅ **已实现**：dispatch 时传唯一 `nonce`，workflow 用 `run-name` 带出：

```yaml
run-name: "${{ inputs.template }}-${{ inputs.nonce }}"
```

dispatch 后按 `display_title` 中的 nonce 匹配认领（`gh_find_run_by_nonce`）。
认领失败不退化为"抓最新一条"，provisioning slot 保留防重复触发。

## 6. Drift 检测

✅ **已实现**：每轮 reconcile 时，对本地标 `running` 但不在 GitHub 活跃列表的 run，
主动查 GitHub 最终状态并更新本地 DB。

```python
gh_active_ids = {gr.get("id") for gr in all_active}
for lr in local_active_all:
    rid = lr.get("run_id")
    if rid and rid not in gh_active_ids:
        final = gh_mod.gh_get_run(rid)
        if final and final.get("status") == "completed":
            db.reconcile_run_status(rid, "completed", final.get("conclusion"), now)
```

## 7. 已解决的历史问题

| 问题 | v0.1.0 | v0.2.0 |
|---|---|---|
| **run ↔ 模板关系** | ❌ 一个 run 承载 atlas1+atlas2 | ✅ 严格 1:1 |
| 状态来源 | 本地 DB | **GitHub 权威，DB 缓存** |
| queued 处理 | ❌ 不看 | ✅ queued 占名额 |
| run 认领 | ❌ per_page=1 抢最新 | ✅ nonce + run-name 精确认领 |
| 多余 run | ❌ 无收敛 | ✅ PRUNE（已启用） |
| run_id 缺失 | ❌ 重复触发 | ✅ provisioning 占坑 |
| DB 状态漂移 | ❌ 无检测 | ✅ drift 检测 |
| 双表分裂 | ❌ Supervisor / Persistent 互不可见 | ✅ Supervisor 停用 |

## 8. 实施记录

| # | 步骤 | 状态 |
|---|---|---|
| 1 | `supervisors.json` 设 `enabled: false` | ✅ 完成 |
| 2 | 新增 `gh_list_active_runs()` | ✅ 完成 |
| 3 | Guard A 改为 `reconcile()` | ✅ 完成 |
| 4 | `provisioning` 状态 + 占坑 | ✅ 完成 |
| 5 | `cancel_intent` 字段 | ✅ 完成 |
| 6 | bootstrap.yml 加 nonce + run-name | ✅ 完成（两个 GitHub 账号） |
| 7 | PRUNE（按模板维度取消多余 run） | ✅ 完成 |
| 8 | Drift 检测 | ✅ 完成 |
| 9 | 合并回 MAINTENANCE.md | ✅ 完成 |

## 9. 参考资料

- 当前实现：`server/persistent.py`、`server/gh.py`、`server/db.py`
- 运维手册：[MAINTENANCE.md](./MAINTENANCE.md) §11
- 配置：`/etc/default/oracle-agent`

## 10. 待修复问题（源码审查 2026-10-09）

> 审查范围：supervisor.py、db.py、persistent.py、template.py、gh.py、config.py、auth.py。
> 以下为源码审查结论，未在生产环境复现。按修复优先级排序。

### 10.1 [P0] 模板执行失败仍可能取消旧 Run

**文件**：`server/persistent.py` 续期逻辑

**问题**：`apply_template()` 返回 `status="failed"` 时不抛异常，续期逻辑未检查返回的 status 就继续取消旧 Run。场景：

```
T0: atlas1 旧 run 健康
T1: 355min 触发轮换
T2: 新 run 启动，agent 注册成功
T3: apply_template() -> 某步失败 -> 返回 status="failed"（不抛异常）
T4: 代码未检查 status，继续取消旧 run
T5: 旧 run 被杀，新 run 模板没跑成功 -> 服务中断
```

**修复方案**：`apply_template()` 返回后检查 status，非 success 则不取消旧 run，记日志，等下一轮重试。

**影响**：直接破坏无缝轮换承诺，可能导致服务中断。**第一个修。**

### 10.2 [P1] 模板并发保护缺少数据库唯一约束

**文件**：`server/db.py`、`server/template.py`

**问题 a**：`template.py` 引用 `sqlite3.IntegrityError` 但未 `import sqlite3`。若真触发该异常，处理分支自身 NameError 崩溃。当前因无唯一约束，该异常不会被触发，bug 一直隐藏。

**问题 b**：`template_runs` 表无部分唯一索引。`get_running_template_run()` 查 + `create_template_run()` 插两步之间无原子性，并发请求可能创建重复 running 记录。

**修复方案**：
1. `template.py` 加 `import sqlite3`
2. 清理历史重复 running 记录后，建部分唯一索引：
   `CREATE UNIQUE INDEX uq_template_runs_one_running ON template_runs(template) WHERE status = running;`
3. `create_template_run()` 捕获 `sqlite3.IntegrityError` 转为业务友好返回

### 10.3 [P2] GitHub 查询失败被当成无活跃 Run ✅ 已修复 (2026-10-09)

**文件**：`server/persistent.py` `_reconcile_with_github()`

**问题**：GitHub API 异常时返回 `[]`，`_do_rotate()` 将其解释为"无活跃 Run"可能触发 `_trigger_new_run()`。语义混淆："查询成功但无结果"与"查询失败"返回值相同。

**兜底分析**：
- provisioning slot：创建前保护，但 slot 在 run 成功建档后清除，GitHub API 随后失败时 slot 已不存在
- heartbeat：创建后检测，能发现 agent 是否在线，但不能阻止 GitHub 上多出 run
- reconcile 巡检：创建后回收，GitHub API 恢复后 prune 多余 run

**结论**：最终能收敛（reconcile 会清理重复 run），但窗口内可能存在重复 runner。**降为 P2 防御性改进。**

**修复方案**：查询失败时返回 `None` 而非 `[]`，调用方遇到 `None` 只记日志等待下一轮，不进入创建分支。

**已实施 (2026-10-09)**：`gh_list_active_runs` 在所有账号失败时返回 `None`（`all_failed` 标志），`_reconcile_with_github` 遇到 `None` 直接 `return None`，`_do_rotate` 跳过该模板本轮。同时修复 `gh.py` 中 5 个重复函数定义（copy-paste artifact，后者静默覆盖前者）。

### 10.4 [P2] PAT 403 处理可能过于激进

**文件**：`server/gh.py`

**问题**：当前 `gh_request()` 对 401/403/404/422 都 failover 到下一个 PAT。但 403 可能是权限不足、组织策略限制或二级限流，不一定是凭据失效。

**实际影响有限**：当前 failover 是无状态的——当次跳过，下一轮仍从第一个 PAT 开始试，不会长期排除。有状态的 Profile 机制（healthy/cooldown/unhealthy）在 supervisor.py 中，但 supervisor 已停用。

**修复方案**（不急）：区分 403 原因——认证失败标记 unhealthy；权限不足记录 repo 级错误；限流结合响应头处理；原因不明的 403 有限重试。

### 10.5 [P2] Supervisor 并发与边界

**文件**：`server/supervisor.py`

**现状**：`supervisors.json` 设 `"enabled": false`，MAINTENANCE.md 明确 atlas1/atlas2 由 Persistent Rotation 负责。supervisor 不在生产执行路径。

**建议**：保留文件但确保不重新启用。不需要为已停用代码修并发问题。若未来需要多进程部署，再统一设计跨进程互斥（Redis 分布式锁或数据库行锁）。

### 10.6 [P1] Secret 管理专项审计

**文件**：`server/db.py`（明文 token 存储）

**待办**：
1. 数据库和备份文件权限确认
2. API 返回值、日志、异常信息是否泄漏 secret
3. 模板执行时 secret 是否只进入必要子进程环境
4. secret 更新后运行中任务和新任务分别采用什么值
5. 删除 secret 后历史模板和运行记录是否受影响

**独立专项，不与轮换重构同时进行。**

### 10.7 修复顺序

| 顺序 | 问题 | 优先级 | 理由 |
|---|---|---|---|
| 1 | 10.1 模板失败仍取消旧 run | P0 | 唯一能直接导致服务中断的问题 |
| 2 | 10.2 DB 唯一约束 + sqlite3 import | P1 | 并发创建破坏核心约束，且有隐藏 bug |
| 3 | 10.6 Secret 审计 | P1 | 独立专项 |
| 4 | 10.3 GitHub 查询失败语义区分 | P2 | ✅ 已修复 (2026-10-09) |
| 5 | 10.4 PAT 403 区分 | P2 | 当前无状态 failover 影响有限 |
| 6 | 10.5 Supervisor 边界 | P2 | 已停用，确保不启动即可 |
