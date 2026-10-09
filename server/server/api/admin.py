"""Read-only admin API (browse via /docs Swagger UI).

All endpoints are read-only. Auth: if ADMIN_TOKEN / ORACLE_ADMIN_TOKEN env is
set, require `Authorization: Bearer <token>`; otherwise open (dev only).
"""
import hmac
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from .. import db
from .. import gh as gh_mod
from ..config import ACCESS_LOG_BACKUPS, ACCESS_LOG_PATH, ONLINE_AFTER_SEC

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_MASKED = "***"


def _mask_task_env(task: dict) -> dict:
    """task 返回前打码 params.env（secret 值绝不外泄）。"""
    t = dict(task)
    params = t.get("params") or {}
    if isinstance(params, dict) and isinstance(params.get("env"), dict):
        params = dict(params)
        params["env"] = {k: _MASKED for k in params["env"]}
        t["params"] = params
    return t


def _is_business_run(run: dict) -> bool:
    """业务 Run = supervisor 拉起的（origin 以 'supervisor:' 开头）。"""
    return str(run.get("origin") or "").startswith("supervisor:")



def _admin_auth_enabled() -> bool:
    # 开关：ADMIN_AUTH_ENABLED=off|false|0 关闭认证（仅调试用，公网暴露时务必打开）
    v = os.environ.get("ADMIN_AUTH_ENABLED", "true").strip().lower()
    return v not in ("0", "false", "off", "no")


def _require_admin(authorization: str | None = Header(default=None)):
    if not _admin_auth_enabled():
        return True
    token = os.environ.get("ORACLE_ADMIN_TOKEN") or os.environ.get("ADMIN_TOKEN")
    if not token:
        return True  # 未配 token 时保持开放（本地/调试）
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "admin token required"},
        )
    got = authorization.split(" ", 1)[1].strip()
    if got != token:
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "bad admin token"},
        )
    return True


@router.get("/health")
def admin_health(_: bool = Depends(_require_admin)):
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@router.get("/agents")
def admin_agents(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    _: bool = Depends(_require_admin),
):
    agents = db.list_agents()
    if status:
        agents = [a for a in agents if a.get("status") == status]
    return agents[:limit]


@router.get("/tasks")
def admin_tasks(
    agent: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    _: bool = Depends(_require_admin),
):
    return [_mask_task_env(t) for t in db.list_tasks(agent_id=agent, status=status)[:limit]]


def _lazy_run_housekeeping():
    """惰性：provisioning 超时落定 + 用 GitHub 状态回写已知 runs。"""
    try:
        db.expire_provisioning(minutes=5)
    except Exception:
        pass
    try:
        pat, owner, repo = _gh_creds_or_raise()
    except HTTPException:
        return
    known = set(db.list_run_ids(limit=200))
    if not known:
        return
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs?per_page=50",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json", "User-Agent": "oracle-server"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        now = datetime.now(timezone.utc).isoformat()
        for r in data.get("workflow_runs", []):
            rid = r["id"]
            if rid in known:
                db.sync_run_from_github(
                    rid, r.get("status") or "unknown", r.get("conclusion"), now
                )
    except Exception:
        pass


@router.get("/runs")
def admin_runs(
    limit: int = Query(100, ge=1, le=500),
    _: bool = Depends(_require_admin),
):
    _lazy_run_housekeeping()
    return [r for r in db.list_runs(limit=limit) if not _is_business_run(r)]


@router.get("/runs/{run_id}")
def admin_run_show(run_id: int, _: bool = Depends(_require_admin)):
    _lazy_run_housekeeping()
    run = db.get_run(run_id)
    if not run or _is_business_run(run):
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "run not found"}
        )
    return run


@router.get("/runs/by-ip/{ip}")
def admin_run_by_ip(ip: str, _: bool = Depends(_require_admin)):
    return [r for r in db.find_runs_by_ip(ip) if not _is_business_run(r)]


@router.get("/active")
def admin_active(_: bool = Depends(_require_admin)):
    """Oracle 侧"正在运行"：在线 agent + 它们关联的 run 记录。"""
    _lazy_run_housekeeping()
    agents = [a for a in db.list_agents() if a.get("online")]
    agent_ids = {a["agent_id"] for a in agents}
    runs = db.list_runs(limit=500)
    active_runs = [r for r in runs if r.get("agent_id") in agent_ids and not _is_business_run(r)]
    return {"online_agents": agents, "active_runs": active_runs}


@router.get("/github/workflows")
def admin_github_workflows(
    status: str = "in_progress",
    _: bool = Depends(_require_admin),
):
    """查 GitHub 侧正在跑的 workflow（需在服务端配置 GH_PAT / GH_OWNER / GH_REPO，
    否则返回 501 说明未配置）。"""
    data = _gh_request_failover(
        "GET",
        f"/repos/{gh_mod.get_accounts()[0][1]}/{gh_mod.get_accounts()[0][2]}/actions/runs?status={status}&per_page=50",
    )
    runs = []
    for r in data.get("workflow_runs", []):
        runs.append(
            {
                "id": r["id"],
                "name": r.get("name"),
                "workflow": r.get("display_title"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "head_branch": r.get("head_branch"),
                "head_sha": r.get("head_sha"),
                "created_at": r.get("created_at"),
                "html_url": r.get("html_url"),
            }
        )
    return {"status": status, "runs": runs}

class TriggerRunRequest(BaseModel):
    count: int = 1  # 触发 N 个 workflow run（0 = 只校验配置，不开跑）


@router.post("/trigger")
def admin_trigger(
    body: TriggerRunRequest,
    _: bool = Depends(_require_admin),
):
    """触发 GitHub Actions workflow（Oracle Runner Bootstrap）。默认 1 个，可指定 count。"""
    count = max(0, min(body.count, 20))
    if not gh_mod.get_accounts():
        raise HTTPException(
            status_code=501,
            detail={
                "code": "not_configured",
                "message": "set GH_PAT / GH_OWNER / GH_REPO env or data/accounts.json on the server",
            },
        )
    dispatched = 0
    for _ in range(count):
        for pat, owner, repo in gh_mod.get_accounts():
            try:
                gh_mod.gh_request(
                    "POST",
                    f"/repos/{owner}/{repo}/actions/workflows/bootstrap.yml/dispatches",
                    data={"ref": "main"},
                )
                dispatched += 1
                break  # success on this account
            except gh_mod.GHRequestError:
                continue
            except Exception:
                continue
    # best-effort: 抓最新的 run_id（dispatch 本身就是 204 无 body），并立即建档
    latest_runs = []
    if count:
        try:
            # Read runs from the dispatch account (the dispatcher that just succeeded).
            data = None
            for pat, owner, repo in gh_mod.get_accounts():
                try:
                    data = gh_mod.gh_request(
                        "GET",
                        f"/repos/{owner}/{repo}/actions/runs?per_page={count}",
                    )
                    if data is not None:
                        _disp_owner = owner
                        break
                except gh_mod.GHRequestError:
                    continue
                except Exception:
                    continue
            if data is None:
                data = {}
            latest_runs = [r["id"] for r in data.get("workflow_runs", [])][:count]
            # 建档：trigger 成功瞬间即拥有 run 档案（status=queued）
            _now_s = datetime.now(timezone.utc).isoformat()
            for _rid in latest_runs:
                try:
                    db.create_run_provisioning(int(_rid), _now_s, github_status="queued")
                except Exception:
                    pass
        except Exception:
            pass
    return {"dispatched": dispatched, "requested": count, "latest_runs": latest_runs}


class ExecRequest(BaseModel):
    run_id: Optional[int] = None      # GitHub Actions Run ID（首选业务标识）
    id: Optional[int] = None          # Oracle runs.id 内部兼容标识
    agent_id: Optional[str] = None    # Runner/Agent 直接标识
    vm_id: Optional[str] = None       # Azure VM 基础设施间接标识
    command: list[str]
    timeout_sec: int = 60


class ExecManyRequest(BaseModel):
    run_ids: Optional[list[int]] = None     # 元素兼容 GitHub run_id / 内部 id
    vm_ids: Optional[list[str]] = None      # Azure VM id 列表
    agent_ids: Optional[list[str]] = None   # Agent 直接标识列表
    all_online: bool = False
    command: list[str]
    timeout_sec: int = 60


def _resolve_exec_target(run_id=None, id=None, agent_id=None, vm_id=None, check_online_agent=True):
    """① 统一解析 target → (agent_id, run_id, resolved_by)
       ② 再判断执行条件：run 属目标走 Run 状态机；agent_id 直连走 Agent 存在/在线检查。

       设计原则：解析与权限/状态判断分离——以后新增 hostname/instance_name 等定位方式，
       只需在这里加一个解析分支，/admin/exec 执行逻辑不变。"""
    given = [k for k, v in (("run_id", run_id), ("id", id), ("agent_id", agent_id), ("vm_id", vm_id))
             if v is not None]
    if len(given) > 1:
        raise HTTPException(
            status_code=400,
            detail={"code": "ambiguous_target", "targets": given,
                    "message": "provide exactly ONE of run_id / id / agent_id / vm_id"},
        )
    if not given:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request",
                    "message": "provide one of run_id / id / agent_id / vm_id"},
        )
    ident = given[0]

    # ---- agent_id 直连：不走 Run 状态机 ----
    if ident == "agent_id":
        ag = db.get_agent(agent_id)
        if not ag:
            raise HTTPException(
                status_code=404,
                detail={"code": "agent_not_found", "message": f"agent not found: {agent_id}"},
            )
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_AFTER_SEC)).isoformat()
        if check_online_agent and not (ag.get("last_seen") and ag["last_seen"] >= cutoff):
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_offline", "agent_id": agent_id,
                        "message": "agent is not online"},
            )
        return {"agent_id": agent_id, "run_id": None, "resolved_by": "agent_id"}

    # ---- run 属目标（run_id / id / vm_id）：走 Run 状态机 ----
    _lazy_run_housekeeping()
    if ident == "vm_id":
        run = db.get_run_by_vm_id(vm_id)
        if not run:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "no run found for vm_id"},
            )
    else:
        num = run_id if ident == "run_id" else id
        run = db.get_run_by_either(num)
        if not run:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found",
                        "message": "run never recorded by Oracle (run_id = GitHub Actions run ID，如 35996818227；内部自增 id 也兼容)"},
            )
    state = run.get("status")
    rid = run.get("run_id")
    aid = run.get("agent_id")
    if state in ("queued", "provisioning"):
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_not_ready", "state": state, "run_id": rid,
                    "message": "GitHub runner has not registered yet"},
        )
    if state == "provisioning_timeout":
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_not_available", "state": state, "run_id": rid},
        )
    if state in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail={"code": "run_not_active", "state": state, "run_id": rid},
        )
    if not aid:
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_not_ready", "state": state, "run_id": rid,
                    "message": "run has no agent"},
        )
    return {"agent_id": aid, "run_id": rid, "resolved_by": ident}


@router.post("/exec")
def admin_exec(
    body: ExecRequest,
    _: bool = Depends(_require_admin),
):
    """在指定 Run / Agent 上执行命令（目标互斥：run_id / id / agent_id / vm_id 四选一）。"""
    if not body.command:
        raise HTTPException(
            status_code=400, detail={"code": "bad_request", "message": "command required (array)"}
        )
    t = _resolve_exec_target(
        run_id=body.run_id, id=body.id, agent_id=body.agent_id, vm_id=body.vm_id
    )
    task = db.create_task(
        t["agent_id"], "exec",
        {"command": body.command, "timeout_sec": body.timeout_sec},
        timeout_sec=body.timeout_sec,
    )
    return {
        "task_id": task["task_id"],
        "run_id": t["run_id"],            # 解析后的 GitHub run_id（agent 直连时为 null）
        "agent_id": t["agent_id"],
        "resolved_by": t["resolved_by"],  # run_id | id | vm_id | agent_id
        "type": "exec",
        "status": task["status"],
    }


@router.post("/exec-many")
def admin_exec_many(
    body: ExecManyRequest,
    _: bool = Depends(_require_admin),
):
    """在多个 Run / 多个 VM / 多个 Agent / 全部在线 Agent 上执行同一命令。"""
    if not body.command:
        raise HTTPException(
            status_code=400, detail={"code": "bad_request", "message": "command required (array)"}
        )
    if not (body.run_ids or body.vm_ids or body.agent_ids or body.all_online):
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": "specify run_ids / vm_ids / agent_ids / all_online"},
        )
    items: list[tuple[str, object]] = []
    if body.all_online:
        items += [("agent_id", a["agent_id"]) for a in db.list_agents() if a.get("online")]
    if body.agent_ids:
        items += [("agent_id", aid) for aid in body.agent_ids]
    if body.run_ids:
        items += [("run_id", rid) for rid in body.run_ids]
    if body.vm_ids:
        items += [("vm_id", vid) for vid in body.vm_ids]

    created, skipped = [], []
    for kind, val in items:
        try:
            if kind == "agent_id":
                t = _resolve_exec_target(agent_id=val)
            elif kind == "run_id":
                t = _resolve_exec_target(run_id=val)
            else:
                t = _resolve_exec_target(vm_id=val)
            task = db.create_task(
                t["agent_id"], "exec",
                {"command": body.command, "timeout_sec": body.timeout_sec},
                timeout_sec=body.timeout_sec,
            )
            created.append({
                "run_id": t["run_id"], "agent_id": t["agent_id"],
                "task_id": task["task_id"], "resolved_by": t["resolved_by"],
                "source": {kind: val},
            })
        except HTTPException as e:
            skipped.append({
                "source": {kind: val},
                "http": e.status_code,
                "code": (e.detail or {}).get("code"),
                "state": (e.detail or {}).get("state"),
            })
    return {"created": len(created), "tasks": created, "skipped": skipped}


@router.get("/tasks/{task_id}")
def admin_task_show(task_id: str, _: bool = Depends(_require_admin)):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "task not found"}
        )
    return _mask_task_env(task)


# 专用 access.log 查询端点：token 走路径，且在 Swagger/OpenAPI 中隐藏
# （include_in_schema=False，路由照常可用）。与 ADMIN_AUTH_ENABLED 开关解耦：
# 只认 LOG_VIEW_TOKEN；未配置该 env 则端点 404 关闭。
@router.get("/logs/{view_token}", include_in_schema=False)
def admin_logs_view(
    view_token: str,
    lines: int = Query(100, ge=1, le=1000),
    q: Optional[str] = None,
    path: Optional[str] = None,
    since: Optional[str] = None,
    file: str = "access.log",
):
    expected = os.environ.get("LOG_VIEW_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "logs endpoint disabled (LOG_VIEW_TOKEN unset)"},
        )
    if not hmac.compare_digest(view_token, expected):
        raise HTTPException(
            status_code=403, detail={"code": "forbidden", "message": "bad log token"}
        )
    allowed = {"access.log"} | {f"access.log.{i}" for i in range(1, ACCESS_LOG_BACKUPS + 1)}
    if file not in allowed:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": f"file must be one of {sorted(allowed)}"},
        )
    full = os.path.join(os.path.dirname(ACCESS_LOG_PATH), file)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "log file not found"})
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.readlines()

    since_dt = None
    if since:
        s = since.strip().lower()
        try:
            if s.endswith("m"):
                since_dt = datetime.now(timezone.utc) - timedelta(minutes=int(s[:-1]))
            elif s.endswith("h"):
                since_dt = datetime.now(timezone.utc) - timedelta(hours=int(s[:-1]))
            elif s.endswith("s"):
                since_dt = datetime.now(timezone.utc) - timedelta(seconds=int(s[:-1]))
            else:
                since_dt = datetime.fromisoformat(s)
        except Exception:
            since_dt = None

    entries = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if since_dt:
            try:
                ts = datetime.fromisoformat(d.get("ts"))
                if ts < since_dt:
                    continue
            except Exception:
                pass
        if q and q not in line:
            continue
        if path and d.get("path") != path:
            continue
        entries.append(d)

    return {"ok": True, "count": len(entries[-lines:]), "entries": entries[-lines:], "file": file}


@router.get("/template-view/{view_token}", include_in_schema=False)
def admin_template_view(
    view_token: str,
    template: Optional[str] = None,
    status: Optional[str] = None,
):
    """非公开只读视图：模板 + 业务 Run(supervisor:*) + template_runs。

    - 复用 LOG_VIEW_TOKEN（与 /logs 一致）；未配置则端点关闭(404)。
    - secret 值永不返回，只给 ref 名。
    """
    expected = os.environ.get("LOG_VIEW_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "endpoint disabled (LOG_VIEW_TOKEN unset)"},
        )
    if not hmac.compare_digest(view_token, expected):
        raise HTTPException(
            status_code=403, detail={"code": "forbidden", "message": "bad token"}
        )

    templs = []
    for t in db.list_templates():
        full = db.get_template(t["name"]) or {}
        steps = full.get("steps") or []
        secret_refs = sorted({ref for s in steps for ref in (s.get("env") or {}).values()})
        templs.append({
            "name": full.get("name") or t["name"],
            "description": full.get("description", ""),
            "enabled": bool(full.get("enabled", True)),
            "steps": steps,
            "secret_refs": secret_refs,
        })

    runs = [r for r in db.list_runs(limit=1000) if _is_business_run(r)]
    if template:
        runs = [r for r in runs if r.get("template") == template]
    if status:
        runs = [r for r in runs if r.get("status") == status]

    t_runs = db.list_template_runs(limit=200, template=template)

    return {"templates": templs, "runs": runs, "template_runs": t_runs}


class CancelRunRequest(BaseModel):
    run_id: Optional[int] = None            # GitHub run ID（单个）
    id: Optional[int] = None                # Oracle 内部 id（单个，兼容）
    vm_id: Optional[str] = None             # Azure VM 反查（单个）
    run_ids: Optional[list[int]] = None     # 多个（元素兼容 GitHub run_id / 内部 id）
    all_in_progress: bool = False           # 或全部 in_progress


def _gh_creds_or_raise():
    if not gh_mod.get_accounts():
        raise HTTPException(
            status_code=501,
            detail={"code": "not_configured", "message": "set GH_PAT / GH_OWNER / GH_REPO env or data/accounts.json on the server"},
        )
    return gh_mod.get_accounts()[0]


def _gh_request_failover(method, path, data=None):
    """Multi-account failover request: primary first, next account on auth/404."""
    try:
        return gh_mod.gh_request(method, path, data=data)
    except gh_mod.GHRequestError as e:
        raise HTTPException(status_code=502, detail={"code": "github_error", "message": str(e)})


@router.post("/cancel")
def admin_cancel(
    body: CancelRunRequest,
    _: bool = Depends(_require_admin),
):
    """停止（取消）GitHub Actions run——生命周期终止主路径，不依赖 agent。"""
    pat, owner, repo = _gh_creds_or_raise()
    targets: list[int] = []
    if body.all_in_progress:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs?status=in_progress&per_page=100",
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json", "User-Agent": "oracle-server"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            targets = [r["id"] for r in data.get("workflow_runs", [])]
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": "github_error", "message": str(e)})
    elif body.run_ids:
        targets = []
        for _rid in body.run_ids:
            _r = db.get_run_by_either(_rid)
            targets.append(int(_r["run_id"]) if _r else _rid)
    else:
        # 单个：支持 run_id / id / vm_id（run 维度；纯 agent_id 无 run 语义，不参与 cancel）
        _single = None
        if body.vm_id is not None:
            _single = db.get_run_by_vm_id(body.vm_id)
        elif body.run_id is not None:
            _single = db.get_run_by_either(body.run_id)
        elif body.id is not None:
            _single = db.get_run_by_either(body.id)
        if _single is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "bad_request",
                        "message": "specify run_id / id / vm_id / run_ids / all_in_progress (run-level; agent_id not valid here)"},
            )
        targets = [int(_single["run_id"])]

    results = []
    for rid in targets:
        cancelled = False
        cancel_err = None
        for _pat, _owner, _repo in gh_mod.get_accounts():
            req = urllib.request.Request(
                f"https://api.github.com/repos/{_owner}/{_repo}/actions/runs/{rid}/cancel",
                method="POST",
                headers={"Authorization": f"Bearer {_pat}", "Accept": "application/vnd.github+json", "User-Agent": "oracle-server"},
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if resp.status in (202, 204):
                        cancelled = True
                        break
            except urllib.error.HTTPError as e:
                cancel_err = e
                if e.code in (401, 403, 404):
                    continue  # try next account
                break  # 409/422 etc -> terminal, don't try other accounts
            except Exception as e:
                cancel_err = e
                continue
        if cancelled:
            results.append({"run_id": rid, "cancelled": True, "http": 202})
        elif isinstance(cancel_err, urllib.error.HTTPError):
            results.append({"run_id": rid, "cancelled": False, "http": cancel_err.code,
                            "reason": "cannot cancel (already completed or not cancellable)" if cancel_err.code in (409, 404) else "github error"})
        else:
            results.append({"run_id": rid, "cancelled": False, "reason": f"http error: {cancel_err}" if cancel_err else "github error"})
    return {"requested": len(targets), "results": results}
