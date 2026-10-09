"""Oracle Agent Server — FastAPI entrypoint.

Run from repo root:
    .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8700
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db, supervisor
from .api import admin, agents, tasks
from .config import (ACCESS_LOG_ALL, ACCESS_LOG_BACKUPS, ACCESS_LOG_MAX_BYTES, ACCESS_LOG_PATH)

log = logging.getLogger("oracle.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    db.reset_stale_assigned()
    # Supervisor（Phase 4A）：配置驱动，无配置则空转；阶段内不启用真实 supervisor 无副作用
    profiles = supervisor.load_accounts()
    # Ensure every account has a bootstrap repo (auto-create via clone+push if missing)
    try:
        from . import gh
        results = gh.ensure_all_bootstrap_repos()
        for (owner, repo), val in results.items():
            log.info("bootstrap %s/%s: %s", owner, repo, val)
    except Exception as e:
        log.warning("bootstrap repo check failed: %s", e)
    sups = [supervisor.Supervisor(cfg, profiles) for cfg in supervisor.load_supervisors()]
    tasks = []
    for su in sups:
        tasks.append(asyncio.create_task(su.run_loop()))
    _app.state.supervisors = sups
    _app.state.profiles = profiles
    # Persistent runner rotation (atlas1/atlas2)
    from .persistent import persistent_runner_loop
    pr_task = asyncio.create_task(persistent_runner_loop())
    tasks.append(pr_task)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(
    title="Oracle Agent Server",
    version="0.1.0",
    lifespan=lifespan,
    description="Runner 控制平台管理 API。协议接口见 agent 标签；管理查询见 admin 标签（需 Bearer admin token）。",
)


@app.exception_handler(HTTPException)
async def _http_exc_handler(_request: Request, exc: HTTPException):
    # 统一错误体：{"error": {"code": ..., "message": ...}}（协议文档 §7）
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


_access_logger = logging.getLogger("oracle.access")
_access_logger.setLevel(logging.INFO)
if not _access_logger.handlers:
    _rlh = RotatingFileHandler(
        ACCESS_LOG_PATH,
        maxBytes=ACCESS_LOG_MAX_BYTES,
        backupCount=ACCESS_LOG_BACKUPS,
        encoding="utf-8",
    )
    _rlh.setFormatter(logging.Formatter("%(message)s"))
    _access_logger.addHandler(_rlh)

_MASK_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "x-api-key",
    "cf-access-client-id", "cf-access-client-secret",
}


def _mask_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        out[k] = "***" if k.lower() in _MASK_HEADERS else v
    return out


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    path = request.url.path
    # 默认跳过 agent 协议高频请求（心跳/pull），保留管理/其他全部
    if path.startswith("/api/v1/agent/") and not ACCESS_LOG_ALL:
        return await call_next(request)
    start = time.time()
    status = 500
    body = None
    # client IP 判定（防伪造）：
    #  - socket 对端非 loopback（=公网直连 REDACTED_SERVER_IP:8700）：client 必须是对端 IP，
    #    x-forwarded-for / cf-connecting-ip 可伪造，一律不信
    #  - socket 对端 loopback（=CF 隧道或本机）：仅当 cdn-loop 含 cloudflare 时才信 CF 头
    _sock_host = request.client.host if request.client else None
    _sock_port = request.client.port if request.client else None
    _loopback = _sock_host in ("127.0.0.1", "::1")
    if not _loopback:
        client = f"{_sock_host}:{_sock_port}"
    else:
        _is_cf = "cloudflare" in (request.headers.get("cdn-loop") or "").lower()
        _cf_ip = (
            request.headers.get("cf-connecting-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or None
        )
        client = _cf_ip if (_is_cf and _cf_ip) else f"{_sock_host}:{_sock_port}"
    # 日志查询端点的 token 不落盘（路径段打码）
    if path.startswith("/api/v1/admin/logs/"):
        path = "/api/v1/admin/logs/<token>"
    if path.startswith("/api/v1/admin/template-view/"):
        path = "/api/v1/admin/template-view/<token>"
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            raw = await request.body()  # 提前读并缓存，下游 pydantic 可复用
            body = raw[:4096].decode("utf-8", "replace")
        except Exception:
            body = None
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "client": client,
            "method": request.method,
            "path": path,
            "query": str(request.query_params),
            "status": status,
            "dur_ms": round((time.time() - start) * 1000, 1),
            "headers": _mask_headers(dict(request.headers)),
            "body": body,
        }
        _access_logger.info(json.dumps(record, ensure_ascii=False))


app.include_router(admin.router)
app.include_router(agents.router)
app.include_router(tasks.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/v1/admin/supervisors-status", include_in_schema=False)
def supervisors_status(request: Request):
    """Supervisor 状态（内部/agentctl，不进 Swagger）。"""
    return {
        "supervisors": supervisor.status_all(request.app.state.supervisors or []),
        "profiles": {
            n: {"state": p.state, "cooldown_until": p.cooldown_until, "last_error": p.last_error}
            for n, p in (request.app.state.profiles or {}).items()
        },
    }
