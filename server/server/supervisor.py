"""Supervisor — 常驻任务守护（Phase 4A 最小闭环）。

职责（Part E / PLAN-oracle-scheduler.md）：
  - WHEN / WHICH：决定"什么时候创建 Runner、用哪个 Credential Profile"
  - 巡检决策（每 check_interval_sec）：
      无运行中 Run                    → 补 1
      最新 Run 已运行 ≥ renew_after_sec → 补 1
      一轮最多补一个
  - 多 PAT 故障转移：401/403 → unhealthy；429 → cooldown(等 reset)；timeout/5xx → 临时失败（不判死）

不涉及 Template（Phase 4B 才接 Agent 注册 → template apply）。
"""
import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger("oracle.supervisor")

# ---------- config ----------

SUPERVISORS_PATH = Path(
    os.environ.get("SUPERVISORS_CONFIG", "/opt/oracle-agent/supervisors.json")
)
ACCOUNTS_PATH = Path(
    os.environ.get("ORACLE_ACCOUNTS_CONFIG", "/opt/oracle-agent/accounts.json")
)
DEFAULT_GITHUB_API = "https://api.github.com"

# ---------- account / credential profile ----------


class Profile:
    """Credential Profile + 健康状态。"""

    def __init__(self, name, owner, pat, max_concurrent_runs=20):
        self.name = name
        self.owner = owner
        self.pat = pat
        self.max_concurrent_runs = max_concurrent_runs
        self.state = "healthy"          # healthy | cooldown | unhealthy
        self.cooldown_until = 0.0       # unix ts（429 reset 时间）
        self.last_error = None

    def available(self) -> bool:
        if self.state == "cooldown":
            return time.time() >= self.cooldown_until
        return self.state == "healthy"

    def mark_success(self):
        self.state = "healthy"
        self.cooldown_until = 0.0
        self.last_error = None

    def mark_unhealthy(self, reason):
        self.state = "unhealthy"
        self.last_error = reason

    def mark_cooldown(self, until_ts, reason):
        self.state = "cooldown"
        self.cooldown_until = until_ts
        self.last_error = reason

    def mark_temporary(self, reason):
        # 网络/5xx 临时失败：不算失效，简单记录，保持现状状态
        self.last_error = reason


def load_accounts() -> dict[str, Profile]:
    """从 accounts.json 加载 profiles；缺失时退化为单账号（env GH_PAT/GH_OWNER）。"""
    profiles: dict[str, Profile] = {}
    if ACCOUNTS_PATH.exists():
        try:
            data = json.loads(ACCOUNTS_PATH.read_text())
            for p in data.get("profiles", []):
                profiles[p["name"]] = Profile(
                    name=p["name"],
                    owner=p["owner"],
                    pat=p["pat"],
                    max_concurrent_runs=int(p.get("max_concurrent_runs", 20)),
                )
        except Exception as e:
            log.warning("accounts.json load failed: %s", e)
    if not profiles:
        pat = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
        owner = os.environ.get("GH_OWNER")
        if pat and owner:
            profiles["primary"] = Profile("primary", owner, pat)
    return profiles


# ---------- github helpers ----------


def _gh_request(profile: Profile, method, url, data=None):
    """发 GitHub API 请求并按 E3 分类健康状态。返回 (response_json_or_None, ok, error_code)。"""
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {profile.pat}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "oracle-supervisor",
            "Content-Type": "application/json",
        },
    )
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, body, timeout=20) as resp:
            profile.mark_success()
            if resp.status == 204:
                return (None, True, None)
            return (json.loads(resp.read().decode("utf-8")), True, None)
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}"
        if e.code == 401:
            profile.mark_unhealthy("auth failed (401)")
        elif e.code == 403:
            profile.mark_unhealthy("permission denied (403)")
        elif e.code == 429:
            reset = e.headers.get("x-ratelimit-reset")
            until = float(reset) if reset else time.time() + 60
            profile.mark_cooldown(until, "rate limit (429)")
        elif e.code >= 500:
            profile.mark_temporary(f"server error (5xx {e.code})")
        else:
            # 4xx 其它：配置/请求错误，不算账号健康问题
            profile.mark_temporary(f"HTTP {e.code}")
        return (None, False, e.code)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        profile.mark_temporary(f"network error: {e}")
        return (None, False, "network")


def query_in_progress(profile: Profile, owner: str, repo: str, workflow_file: str):
    """查询指定 repo/workflow 的 in_progress runs（按最新在前）。"""
    q = urllib.parse.urlencode(
        {"status": "in_progress", "path": workflow_file, "per_page": 50}
    )
    url = f"{DEFAULT_GITHUB_API}/repos/{owner}/{repo}/actions/runs?{q}"
    data, ok, code = _gh_request(profile, "GET", url)
    if not ok or not data:
        return None
    runs = []
    for r in data.get("workflow_runs", []):
        runs.append(
            {
                "id": r["id"],
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "html_url": r.get("html_url"),
            }
        )
    return runs


def dispatch_run(profile: Profile, owner: str, repo: str, workflow_file: str, ref: str = "main"):
    """触发 workflow_dispatch；成功返回 (run_id|None)。dispatch 本身 204 无 body。"""
    url = (
        f"{DEFAULT_GITHUB_API}/repos/{owner}/{repo}/actions/workflows/"
        f"{urllib.parse.quote(workflow_file, safe='/')}/dispatches"
    )
    data, ok, code = _gh_request(profile, "POST", url, {"ref": ref})
    if ok:
        return (None, True)
    return (None, False)


# ---------- supervisors ----------


def load_supervisors() -> list[dict]:
    if not SUPERVISORS_PATH.exists():
        return []
    try:
        data = json.loads(SUPERVISORS_PATH.read_text())
        return data.get("supervisors", [])
    except Exception as e:
        log.warning("supervisors.json load failed: %s", e)
        return []


def _iso_to_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class Supervisor:
    """单个 supervisor 的巡检循环。"""

    def __init__(self, cfg: dict, profiles: dict[str, Profile]):
        self.cfg = cfg
        self.name = cfg.get("name", "unnamed")
        self.repo = cfg.get("repo", "")
        self.workflow_file = cfg.get("workflow_file", ".github/workflows/bootstrap.yml")
        self.ref = cfg.get("ref", "main")
        self.interval = float(cfg.get("check_interval_sec", 60))
        self.min_running = int(cfg.get("min_running", 1))
        self.renew_after = float(cfg.get("renew_after_sec", 21300))  # 5h55m
        self.account_names = cfg.get("accounts") or ["primary"]
        self.template = cfg.get("template")  # Phase 4B 使用，4A 忽略
        self.enabled = bool(cfg.get("enabled", True))
        self.profiles = profiles

        # 可观测状态（Phase 5 再做完整视图，这里先留着）
        self.last_check_at = None
        self.last_trigger_at = None
        self.last_trigger_account = None
        self.last_action = "init"
        self.last_error = None
        self.current_runs = []

    def _pick_profile(self):
        """按 accounts 顺序选第一个可用 profile。"""
        for name in self.account_names:
            p = self.profiles.get(name)
            if p and p.available():
                return name, p
        return None, None

    async def patrol_once(self):
        """单次巡检（E2 决策）；一轮最多补一个。"""
        self.last_check_at = datetime.now(timezone.utc).isoformat()
        owner, repo = self.repo.split("/") if "/" in self.repo else (None, self.repo)
        if not owner:
            self.last_error = "repo must be owner/repo"
            self.last_action = "config_error"
            return

        aname, prof = self._pick_profile()
        if not prof:
            self.last_action = "no_account"
            self.last_error = f"no available profile in {self.account_names}"
            return

        runs = query_in_progress(prof, owner, repo, self.workflow_file)
        if runs is None:
            self.last_action = "query_failed"
            self.last_error = prof.last_error
            return
        self.current_runs = runs

        # E2 决策
        need_trigger = False
        if len(runs) < self.min_running:
            need_trigger = True
            action = "no_run"
        else:
            latest = max(runs, key=lambda r: _iso_to_ts(r["created_at"]))
            age = time.time() - _iso_to_ts(latest["created_at"])
            if age >= self.renew_after:
                need_trigger = True
                action = "renew"
            else:
                action = "wait"
                self.last_action = action
                self.last_error = None
                return

        if not need_trigger:
            return

        # 触发（选当前 profile；失败则按序换下一个，一轮最多一次成功触发）
        for name in self.account_names:
            p = self.profiles.get(name)
            if not p or not p.available():
                if name == self.account_names[-1]:
                    break
                continue
            _rid, ok = dispatch_run(p, owner, repo, self.workflow_file, self.ref)
            if ok:
                self.last_trigger_at = datetime.now(timezone.utc).isoformat()
                self.last_trigger_account = name
                self.last_action = action
                self.last_error = None
                # 建档：dispatch 后抓最新 run_id（尽力而为）
                try:
                    recent = query_in_progress(p, owner, repo, self.workflow_file) or []
                    if recent and recent[0]["id"]:
                        db.create_run_provisioning(
                            int(recent[0]["id"]),
                            datetime.now(timezone.utc).isoformat(),
                            github_status="queued",
                            template=self.template,
                            origin=f"supervisor:{self.name}",
                        )
                except Exception:
                    pass
                return
            # 该 profile 触发失败 → 下一个（状态已按 E3 分级更新）
        self.last_action = "trigger_failed"
        self.last_error = f"all accounts failed for {action}"

    async def run_loop(self):
        if not self.enabled:
            log.info("supervisor %s disabled", self.name)
            return
        log.info("supervisor %s started (repo=%s workflow=%s)", self.name, self.repo, self.workflow_file)
        while True:
            try:
                await asyncio.to_thread(self.patrol_once)
            except Exception as e:
                self.last_action = "error"
                self.last_error = str(e)
                log.exception("supervisor %s patrol error", self.name)
            await asyncio.sleep(self.interval)


def status_all(supervisors: list[Supervisor]) -> list[dict]:
    out = []
    for s in supervisors:
        out.append(
            {
                "name": s.name,
                "enabled": s.enabled,
                "repo": s.repo,
                "workflow_file": s.workflow_file,
                "last_check_at": s.last_check_at,
                "last_trigger_at": s.last_trigger_at,
                "last_trigger_account": s.last_trigger_account,
                "last_action": s.last_action,
                "last_error": s.last_error,
                "current_runs": s.current_runs,
            }
        )
    return out