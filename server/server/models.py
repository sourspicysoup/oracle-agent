"""Pydantic request models for Oracle Agent Protocol v1."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    agent_id: Optional[str] = None
    token: Optional[str] = None  # required when agent_id already exists (re-register)
    name: Optional[str] = None
    version: Optional[str] = None
    capabilities: Optional[List[str]] = None
    meta: Optional[Dict[str, Any]] = None


class HeartbeatRequest(BaseModel):
    agent_id: str
    status: str = "online"  # starting | online | degraded
    version: Optional[str] = None
    capabilities: Optional[List[str]] = None
    load: Optional[Dict[str, Any]] = None


class PullRequest(BaseModel):
    agent_id: str
    timeout_sec: int = 30


class ResultRequest(BaseModel):
    agent_id: str
    task_id: str
    status: str  # success | failed | timeout | rejected
    output: Optional[Any] = None
    error: Optional[Any] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ReportRequest(BaseModel):
    agent_id: str
    event: str
    data: Optional[Any] = None
    ts: Optional[str] = None