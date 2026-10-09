"""Oracle Agent Server — v1 configuration."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ORACLE_DB", str(BASE_DIR / "data" / "oracle.db")))

HOST = os.environ.get("ORACLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORACLE_PORT", "8700"))

# Protocol constants (see docs/oracle-agent-protocol-v1.md)
HEARTBEAT_INTERVAL_DEFAULT = 60
PULL_TIMEOUT_DEFAULT = 30
PULL_TIMEOUT_MAX = 60
ASSIGNED_EXPIRY_SEC = 900  # assigned but untouched for 15 min -> reset to pending (redelivery)
ONLINE_AFTER_SEC = 180     # last_seen within this window => agent considered online

# Access log (JSON lines, size-based rotation)
ACCESS_LOG_PATH = os.environ.get("ACCESS_LOG_PATH", str(BASE_DIR / "data" / "access.log"))
ACCESS_LOG_MAX_BYTES = int(os.environ.get("ACCESS_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
ACCESS_LOG_BACKUPS = int(os.environ.get("ACCESS_LOG_BACKUPS", "5"))
# 默认跳过 /api/v1/agent/* 协议频繁请求（心跳/pull）；要全量记录设 ACCESS_LOG_ALL=true
ACCESS_LOG_ALL = os.environ.get("ACCESS_LOG_ALL", "false").strip().lower() in ("1", "true", "yes")