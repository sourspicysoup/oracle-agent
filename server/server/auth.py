"""Bearer-token auth helper for Agent API endpoints."""
from fastapi import HTTPException

from . import db


def require_auth(agent_id: str, authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "missing bearer token"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not db.verify_token(agent_id, token):
        raise HTTPException(
            status_code=403,
            detail={"code": "forbidden", "message": "token mismatch for agent"},
        )