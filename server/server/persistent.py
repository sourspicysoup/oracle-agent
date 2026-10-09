"""Persistent runner rotation with GitHub-side state machine.

Implements RUN_LIFECYCLE.md: GitHub is the authority for run lifecycle,
local heartbeat is auxiliary. Each template maintains exactly 1 run (1:1).

Key changes from old version:
- dispatch with nonce + template, claim run by nonce (no more per_page=1 grab)
- reconcile with GitHub before deciding to trigger
- PRUNE excess/orphan runs
- provisioning status occupies slot before run_id is known
"""
import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import db
from .template import apply_template, TemplateError

log = logging.getLogger("oracle.persistent")
if not log.handlers:
    import sys as _sys
    _h = logging.StreamHandler(_sys.stderr)
    _h.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ── 配置 ──
PERSISTENT_TEMPLATES = [
    t.strip()
    for t in os.environ.get("PERSISTENT_TEMPLATES", "").split(",")
    if t.strip()
]
ROTATION_AGE_MIN = int(os.environ.get("ROTATION_AGE_MIN", "355"))
ROTATION_CHECK_INTERVAL = int(os.environ.get("ROTATION_CHECK_INTERVAL", "300"))
AGENT_ONLINE_TIMEOUT = int(os.environ.get("AGENT_ONLINE_TIMEOUT", "600"))
ONLINE_AFTER_SEC = 180
STALE_RUN_MAX_AGE_HOURS = float(os.environ.get("STALE_RUN_MAX_AGE_HOURS", "6.5"))
# PRUNE dry-run: only log, don't actually cancel
PRUNE_DRY_RUN = os.environ.get("PRUNE_DRY_RUN", "1") == "1"

_rotating: set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_agent_online(agent_id: str) -> bool:
    agent = db.get_agent(agent_id)
    if not agent:
        return False
    last_seen = agent.get("last_seen")
    if not last_seen:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_AFTER_SEC)).isoformat()
    return last_seen >= cutoff


# ── GitHub reconciliation ──

def _reconcile_with_github(template: str) -> list[dict]:
    """Pull GitHub facts, sync local DB, return active runs for this template.

    This is the core of the GitHub-side state machine:
    1. List all active runs from GitHub (queued + in_progress)
    2. For each, update local DB status
    3. Filter to runs owned by this template (by display_title match)
    4. Return the list of active runs belonging to this template
    """
    from . import gh as gh_mod

    # 1. Fetch all active runs from GitHub (one request, no status filter)
    try:
        all_active = gh_mod.gh_list_active_runs(per_page=30)
    except Exception as e:
        log.warning("persistent: gh_list_active_runs failed: %s", e)
        return []

    now = _now_iso()

    # 2. Reconcile local DB with GitHub facts
    for gr in all_active:
        rid = gr.get("id")
        if rid:
            try:
                db.reconcile_run_status(
                    rid, gr.get("status", "queued"),
                    gr.get("conclusion"), now,
                )
            except Exception:
                pass  # best-effort sync

    # 2b. Drift detection: local runs active in DB but NOT in GitHub active list
    #     Query GitHub for final status and update local DB
    gh_active_ids = {gr.get("id") for gr in all_active if gr.get("id")}
    try:
        local_active_all = db.get_active_runs_by_template(template)
        for lr in local_active_all:
            rid = lr.get("run_id")
            if rid and rid not in gh_active_ids:
                try:
                    final = gh_mod.gh_get_run(rid)
                    if final and final.get("status") == "completed":
                        db.reconcile_run_status(
                            rid, "completed",
                            final.get("conclusion"), now,
                        )
                        log.info("persistent: drift fix — run %d completed on GitHub (conclusion=%s), updated local DB",
                                 rid, final.get("conclusion"))
                    elif final is None:
                        # GitHub API 查不到这个 run（账户 suspended / PAT 失效）
                        # 回退到 agent 心跳判断
                        agent_id = lr.get("agent_id")
                        if agent_id and not _is_agent_online(agent_id):
                            # agent 心跳已断 → run 已死，标记为 cancelled
                            db.reconcile_run_status(rid, "completed", "cancelled", now)
                            log.info("persistent: drift — run %d 不可达且 agent %s 离线，标记 cancelled",
                                     rid, agent_id)
                        # else: agent 还在心跳 → 保持 running，等它自己死
                except Exception:
                    pass
    except Exception:
        pass


    # 3. Filter to runs owned by this template
    #    A run is "owned" by a template if its display_title starts with the template name
    #    (run-name format: "{template}-{nonce}")
    mine = []
    for gr in all_active:
        title = gr.get("display_title") or gr.get("name") or ""
        if title.startswith(template + "-") or title == template:
            mine.append(gr)

    # Also check local DB for active runs with this template (covers runs where
    # GitHub title didn't match but local DB has the assignment)
    local_active = db.get_active_runs_by_template(template)
    local_run_ids = {r["run_id"] for r in local_active if r.get("run_id")}
    for gr in all_active:
        if gr.get("id") in local_run_ids and gr not in mine:
            mine.append(gr)

    return mine


def _trigger_new_run(template_name: str) -> tuple[int, str] | None:
    """Trigger new GitHub Actions run with nonce, return (run_id, nonce) or None.

    Uses provisioning slot to prevent duplicate triggers even if run_id capture fails.
    """
    from . import gh as gh_mod

    nonce = uuid.uuid4().hex[:8]
    now = _now_iso()

    # 1. Insert provisioning record (occupies slot, run_id NULL)
    row_id = db.insert_provisioning(template_name, nonce, now)

    # 2. Dispatch with nonce + template
    if not gh_mod.gh_dispatch_workflow(template_name, nonce):
        log.warning("persistent: dispatch failed for %s (nonce=%s)", template_name, nonce)
        return None

    # 3. Find the run by nonce in display_title
    #    GitHub may take a few seconds to create the run, poll with retries
    run_id = None
    for attempt in range(12):  # ~60s max
        time.sleep(5)
        gr = gh_mod.gh_find_run_by_nonce(nonce, template_name, per_page=10)
        if gr:
            run_id = int(gr["id"])
            # 4. Update provisioning record with real run_id
            db.update_run_id(row_id, run_id)
            # Sync status from GitHub
            db.reconcile_run_status(
                run_id, gr.get("status", "queued"),
                gr.get("conclusion"), now,
            )
            log.info("persistent: claimed run %d for %s (nonce=%s, title=%s)",
                     run_id, template_name, nonce, gr.get("display_title"))
            return (run_id, nonce)

    log.warning("persistent: could not claim run for %s (nonce=%s) after 60s — "
                "provisioning slot remains to prevent duplicate trigger",
                template_name, nonce)
    return None


def _wait_for_agent(run_id: int, timeout: int = 600) -> str | None:
    """Poll for new agent to register and come online."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = db.get_run(run_id)
        if run and run.get("agent_id"):
            agent_id = run["agent_id"]
            if _is_agent_online(agent_id):
                return agent_id
        time.sleep(5)
    return None


def _prune_excess_runs(template: str, active_runs: list[dict]) -> None:
    """Cancel excess runs for this template (keep newest, cancel rest).

    Also cancel orphan runs (no template assignment, from persistent_rotation).
    DRY_RUN mode: only log, don't actually cancel.
    """
    from . import gh as gh_mod

    if len(active_runs) <= 1:
        return

    # Sort by created_at descending, keep newest
    sorted_runs = sorted(
        active_runs,
        key=lambda r: r.get("created_at") or "",
        reverse=True,
    )
    keep = sorted_runs[0]
    cancel = sorted_runs[1:]

    for gr in cancel:
        rid = gr.get("id")
        log.info("persistent: PRUNE %s — excess run %d (title=%s, status=%s)%s",
                 template, rid, gr.get("display_title"), gr.get("status"),
                 " [DRY-RUN]" if PRUNE_DRY_RUN else "")
        if not PRUNE_DRY_RUN:
            gh_mod.gh_cancel_run(rid)
            db.mark_cancel_intent(rid)


def _prune_orphan_runs() -> None:
    """Cancel orphan runs (persistent_rotation origin, no template, still active)."""
    from . import gh as gh_mod

    orphans = db.get_orphan_runs()
    if not orphans:
        return

    for run in orphans:
        rid = run.get("run_id")
        if not rid:
            continue
        log.info("persistent: PRUNE orphan run %d (status=%s, triggered=%s)%s",
                 rid, run.get("status"), run.get("triggered_at"),
                 " [DRY-RUN]" if PRUNE_DRY_RUN else "")
        if not PRUNE_DRY_RUN:
            gh_mod.gh_cancel_run(rid)
            db.mark_cancel_intent(rid)


def _check_and_rotate(template_name: str):
    """Check single template, rotate if needed. GitHub is authority."""
    # Guard: already rotating
    if template_name in _rotating:
        log.info("persistent: %s already rotating, skip", template_name)
        return
    _rotating.add(template_name)
    try:
        _do_rotate(template_name)
    finally:
        _rotating.discard(template_name)


def _do_rotate(template_name: str):
    """Main decision logic. GitHub-authoritative per RUN_LIFECYCLE.md §4."""

    # 1. Reconcile with GitHub — get active runs owned by this template
    active = _reconcile_with_github(template_name)

    # 2. PRUNE: if more than 1 active run for this template, cancel excess
    if len(active) > 1:
        log.warning("persistent: %s has %d active runs (violates 1:1), pruning",
                    template_name, len(active))
        _prune_excess_runs(template_name, active)
        # Re-fetch after prune (or keep going with the survivor)
        active = active[:1]  # optimistic; next cycle will re-verify

    # 3. Decision
    if len(active) == 0:
        # No active run for this template on GitHub → TRIGGER
        log.info("persistent: %s has no active run on GitHub — triggering", template_name)
        result = _trigger_new_run(template_name)
        if not result:
            log.warning("persistent: trigger failed for %s, will retry next cycle",
                        template_name)
            return
        new_run_id, nonce = result
        log.info("persistent: triggered new run %d for %s (nonce=%s)",
                 new_run_id, template_name, nonce)

        # 4. Wait for agent online
        new_agent_id = _wait_for_agent(new_run_id, timeout=AGENT_ONLINE_TIMEOUT)
        if not new_agent_id:
            log.warning("persistent: no agent online for run %d within %ds",
                        new_run_id, AGENT_ONLINE_TIMEOUT)
            return
        log.info("persistent: agent %s online for run %d", new_agent_id, new_run_id)

        # 5. Apply template
        try:
            def on_step(i, total, name, task_id, status):
                log.info("persistent: [%s] step %d/%d %s (task %s)",
                         status, i + 1, total, name, task_id[:12])
            res = apply_template(
                new_agent_id, template_name,
                run_id=new_run_id, on_step=on_step,
            )
            log.info("persistent: template %s applied to agent %s — status: %s",
                     template_name, new_agent_id, res.get("status"))
        except TemplateError as e:
            log.error("persistent: template apply failed for %s on agent %s: %s",
                      template_name, new_agent_id, e)
        except Exception as e:
            log.exception("persistent: unexpected error applying template %s: %s",
                          template_name, e)
        return

    # We have exactly 1 active run
    run = active[0]
    run_id = run.get("id")
    github_status = run.get("status", "")
    created_at = run.get("created_at")

    # Check age (GitHub run age, approaching 6h hard limit)
    if created_at:
        try:
            created_ts = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
            age_min = (time.time() - created_ts) / 60

            if age_min >= ROTATION_AGE_MIN:
                log.info("persistent: %s run %d age %.1fmin >= %dmin — RENEW",
                         template_name, run_id, age_min, ROTATION_AGE_MIN)
                # RENEW: start new, wait for agent, then cancel old (seamless)
                result = _trigger_new_run(template_name)
                if result:
                    new_run_id, _ = result
                    new_agent_id = _wait_for_agent(new_run_id, timeout=AGENT_ONLINE_TIMEOUT)
                    if new_agent_id:
                        try:
                            def on_step(i, total, name, task_id, status):
                                log.info("persistent: [%s] step %d/%d %s (task %s)",
                                         status, i + 1, total, name, task_id[:12])
                            apply_template(new_agent_id, template_name,
                                          run_id=new_run_id, on_step=on_step)
                            log.info("persistent: RENEW OK — new run %d, canceling old %d",
                                     new_run_id, run_id)
                            from . import gh as gh_mod
                            if not PRUNE_DRY_RUN:
                                gh_mod.gh_cancel_run(run_id)
                                db.mark_cancel_intent(run_id)
                        except Exception as e:
                            log.error("persistent: RENEW apply failed: %s", e)
                return
        except Exception as e:
            log.warning("persistent: age parse error for %s: %s", template_name, e)

    # Check heartbeat (auxiliary: agent may be dead while GitHub says in_progress)
    local_run = db.get_run(run_id) if run_id else None
    agent_id = local_run.get("agent_id") if local_run else None

    if agent_id and not _is_agent_online(agent_id):
        log.info("persistent: %s run %d agent %s offline — RENEW_EARLY",
                 template_name, run_id, agent_id)
        # RENEW_EARLY: cancel old first, then start new (accept brief interruption)
        from . import gh as gh_mod
        if not PRUNE_DRY_RUN:
            gh_mod.gh_cancel_run(run_id)
            db.mark_cancel_intent(run_id)
        result = _trigger_new_run(template_name)
        if result:
            new_run_id, _ = result
            new_agent_id = _wait_for_agent(new_run_id, timeout=AGENT_ONLINE_TIMEOUT)
            if new_agent_id:
                try:
                    def on_step2(i, total, name, task_id, status):
                        log.info("persistent: [%s] step %d/%d %s (task %s)",
                                 status, i + 1, total, name, task_id[:12])
                    apply_template(new_agent_id, template_name,
                                  run_id=new_run_id, on_step=on_step2)
                    log.info("persistent: RENEW_EARLY OK — new run %d", new_run_id)
                except Exception as e:
                    log.error("persistent: RENEW_EARLY apply failed: %s", e)
        return

    # All good
    log.info("persistent: %s OK — run %d (status=%s, agent=%s)",
             template_name, run_id, github_status,
             agent_id or "pending")


async def persistent_runner_loop():
    """Background task: keep configured templates' runners alive."""
    if not PERSISTENT_TEMPLATES:
        log.info("persistent_runner_loop: PERSISTENT_TEMPLATES not set, idle")
        return

    log.info(
        "persistent_runner_loop started: templates=%s, age_threshold=%dmin, "
        "check_interval=%ds, prune_dry_run=%s",
        PERSISTENT_TEMPLATES, ROTATION_AGE_MIN, ROTATION_CHECK_INTERVAL,
        PRUNE_DRY_RUN,
    )

    while True:
        # 兜底：回收超时仍非终态的 run
        try:
            reaped = db.reap_stale_runs(STALE_RUN_MAX_AGE_HOURS)
            if reaped:
                log.warning("persistent: reaped %d stale run(s) older than %sh",
                            reaped, STALE_RUN_MAX_AGE_HOURS)
        except Exception:
            log.exception("persistent: reap_stale_runs failed")

        # PRUNE orphan runs (no template, from persistent_rotation)
        try:
            _prune_orphan_runs()
        except Exception:
            log.exception("persistent: prune_orphan_runs failed")

        for template_name in PERSISTENT_TEMPLATES:
            asyncio.create_task(asyncio.to_thread(_check_and_rotate, template_name))
        await asyncio.sleep(ROTATION_CHECK_INTERVAL)
