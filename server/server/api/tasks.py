"""Task endpoints: pull (long-poll) / result."""
import asyncio
import time

from fastapi import APIRouter, Header, HTTPException

from .. import db
from ..auth import require_auth
from ..config import PULL_TIMEOUT_MAX
from ..models import PullRequest, ResultRequest

router = APIRouter(prefix="/api/v1/agent", tags=["task"])

ALLOWED_RESULT_STATUS = ("success", "failed", "timeout", "rejected")


@router.post("/pull")
async def pull(body: PullRequest, authorization: str | None = Header(default=None)):
    require_auth(body.agent_id, authorization)
    timeout = max(1, min(body.timeout_sec, PULL_TIMEOUT_MAX))
    deadline = time.monotonic() + timeout
    while True:
        task = db.claim_next_pending(body.agent_id)
        if task:
            return {"task": task}
        if time.monotonic() >= deadline:
            return {"task": None}
        await asyncio.sleep(1)


@router.post("/result")
def result(body: ResultRequest, authorization: str | None = Header(default=None)):
    require_auth(body.agent_id, authorization)
    if body.status not in ALLOWED_RESULT_STATUS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_request",
                "message": f"status must be one of {ALLOWED_RESULT_STATUS}",
            },
        )
    res = db.complete_task(
        body.agent_id,
        body.task_id,
        body.status,
        body.output,
        body.error,
        body.started_at,
        body.finished_at,
    )
    if not res["ok"]:
        if res["reason"] == "not_found":
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "task not found for this agent"},
            )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": "task already finalized with a different status",
            },
        )
    return {"ok": True}