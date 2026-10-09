"""Agent identity endpoints: register / heartbeat / report."""
import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException

from .. import db
from ..auth import require_auth
from ..config import HEARTBEAT_INTERVAL_DEFAULT
from ..models import HeartbeatRequest, RegisterRequest, ReportRequest

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


def _server_time() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/register")
def register(body: RegisterRequest):
    agent_id = (body.agent_id or "").strip()
    if not agent_id:
        # Agent did not name itself — assign a random one.
        agent_id = "runner-" + secrets.token_hex(4)

    # If the id already exists, registration is only valid with the current
    # token (prevents stealing an agent's identity).
    if db.get_token_hash(agent_id) is not None:
        if not body.token or not db.verify_token(agent_id, body.token):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "forbidden",
                    "message": "agent_id already exists; valid token required to re-register",
                },
            )

    rec = db.register_agent(
        agent_id,
        body.name,
        body.version,
        body.capabilities,
        body.meta,
    )
    try:
        _rid = int((body.meta or {}).get("run_id"))
    except (TypeError, ValueError):
        _rid = None
    if _rid:
        db.verify_registered_run_agent(_rid, body.meta or {}, rec["agent_id"], _server_time())
    return {
        "agent_id": rec["agent_id"],
        "token": rec["token"],  # None on re-register (agent keeps local token)
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_DEFAULT,
        "server_time": _server_time(),
    }


@router.post("/heartbeat")
def heartbeat(body: HeartbeatRequest, authorization: str | None = Header(default=None)):
    require_auth(body.agent_id, authorization)
    status = body.status if body.status in ("starting", "online", "degraded") else "online"
    updated = db.heartbeat(
        body.agent_id, status, body.version, body.capabilities, body.load
    )
    if not updated:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "unknown agent — register first"},
        )
    return {
        "ok": True,
        "server_time": _server_time(),
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_DEFAULT,
    }


@router.post("/report")
def report(body: ReportRequest, authorization: str | None = Header(default=None)):
    require_auth(body.agent_id, authorization)
    agent = db.get_agent(body.agent_id)
    if not agent:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "unknown agent — register first"},
        )
    # v1: keep a small event transcript inside agent meta (queryable via
    # agentctl list). A dedicated event log table is a later version.
    meta = json.loads(agent.get("meta") or "{}")
    meta.setdefault("events", []).append(
        {"event": body.event, "data": body.data, "ts": body.ts or _server_time()}
    )
    meta["events"] = meta["events"][-20:]
    db.update_agent_meta(body.agent_id, meta)
    return {"ok": True}