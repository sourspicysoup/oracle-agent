"""GitHub account discovery + HTTP helpers with failover support.

Account resolution order:
  1. Primary: environment GH_PAT / GH_OWNER / GH_REPO (or GITHUB_TOKEN fallback).
  2. Backup:  /opt/oracle-agent/data/accounts.json -- profiles list.
Every profile needs: name, owner, pat, repo (defaults to bootstrap repo name).

`gh_request` tries accounts in order and returns the first successful result;
on auth/permission failures (401/403) it advances to the next account.

`ensure_all_bootstrap_repos` checks every account has its bootstrap repo;
if missing, creates an independent copy via clone+push (NOT a GitHub fork).
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("oracle.gh")

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = Path(os.environ.get("ORACLE_ACCOUNTS", str(BASE_DIR / "data" / "accounts.json")))

BOOTSTRAP_REPO = "oracle-agent-bootstrap"


def _env_account():
    pat = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    owner = os.environ.get("GH_OWNER")
    repo = os.environ.get("GH_REPO") or BOOTSTRAP_REPO
    if pat and owner:
        return (pat, owner, repo)
    return None


def _file_accounts():
    """Load backup profiles from accounts.json (same schema as supervisor)."""
    out = []
    try:
        data = json.loads(ACCOUNTS_PATH.read_text())
    except Exception:
        return out
    for p in data.get("profiles", []):
        pat = p.get("pat")
        owner = p.get("owner")
        if pat and owner:
            repo = p.get("repo") or BOOTSTRAP_REPO
            out.append((pat, owner, repo))
    return out


def get_accounts():
    """[(pat, owner, repo), ...] primary first, backups after; dedup by (owner,repo)."""
    accs = []
    seen = set()
    for acc in ([_env_account()] if _env_account() else []) + _file_accounts():
        key = (acc[1], acc[2])
        if key not in seen:
            seen.add(key)
            accs.append(acc)
    return accs


def gh_request(method, path, data=None, accept_json=True):
    """Try all accounts in order; first 2xx wins.

    Host-relative base: https://api.github.com
    Returns parsed JSON (or None for 204 / non-JSON) on success.
    Raises GHRequestError when every account failed.
    """
    last = None
    for pat, owner, repo in get_accounts():
        url = f"https://api.github.com{path}"
        headers = {
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "oracle-server",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            req = urllib.request.Request(
                url,
                method=method,
                data=json.dumps(data).encode() if data is not None else None,
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status in (202, 204) or not accept_json:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403, 404, 422):
                continue  # bad creds / forbidden / no repo on this account -> next
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"network: {e}"
            continue
    raise GHRequestError(f"all GitHub accounts failed: {last}")


class GHRequestError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Bootstrap repo auto-provisioning (clone + push, NOT fork)                    #
# --------------------------------------------------------------------------- #

def _gh_api(pat, method, path, data=None, timeout=20):
    """Low-level GitHub API call with a specific PAT (not failover)."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "oracle-server",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def repo_exists(pat: str, owner: str, repo: str) -> bool:
    """Check if {owner}/{repo} exists AND has content (at least 1 commit)."""
    try:
        _gh_api(pat, "GET", f"/repos/{owner}/{repo}/commits?per_page=1")
        return True
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            # 404 = repo doesn't exist; 409 = repo exists but empty (Git GitHub API behavior)
            return False
        raise


def create_repo(pat: str, name: str, private: bool = False) -> dict:
    """Create a new empty repo under the PAT owner's account."""
    return _gh_api(pat, "POST", "/user/repos",
                   {"name": name, "private": private, "auto_init": False})


def ensure_bootstrap_repo(pat, owner, repo,
                          source_pat, source_owner, source_repo,
                          private=False):
    """Ensure {owner}/{repo} exists. If not, clone from source and push.

    Returns "exists" or "created".  Raises on error.
    """
    if repo_exists(pat, owner, repo):
        log.info("bootstrap %s/%s already exists", owner, repo)
        return "exists"

    log.info("bootstrap %s/%s missing — creating (clone+push from %s/%s)",
             owner, repo, source_owner, source_repo)

    # 1. Create empty target repo (tolerate 422 = repo already exists but empty)
    try:
        create_repo(pat, repo, private=private)
        log.info("created empty repo %s/%s", owner, repo)
    except urllib.error.HTTPError as e:
        if e.code == 422:
            log.info("repo %s/%s already exists (empty), skipping creation", owner, repo)
        else:
            raise

    # 2. Clone source → push to target
    tmpdir = tempfile.mkdtemp(prefix=f"bootstrap-{owner}-")
    try:
        source_url = f"https://{source_pat}@github.com/{source_owner}/{source_repo}"
        target_url = f"https://{pat}@github.com/{owner}/{repo}"

        r = subprocess.run(
            ["git", "clone", source_url, tmpdir],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed: {r.stderr.strip()}")

        r = subprocess.run(
            ["git", "remote", "set-url", "origin", target_url],
            cwd=tmpdir, capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git remote set-url failed: {r.stderr.strip()}")

        # Detect default branch
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=tmpdir, capture_output=True, text=True, timeout=5,
        )
        branch = r.stdout.strip() or "main"

        r = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=tmpdir, capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git push failed: {r.stderr.strip()}")

        log.info("cloned %s/%s → %s/%s (branch %s), pushed",
                 source_owner, source_repo, owner, repo, branch)
        return "created"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_all_bootstrap_repos(private=False) -> dict:
    """For every configured account, ensure its bootstrap repo exists.

    Strategy: find the first account that already has the repo, use it as
    clone source for accounts that don't.  If nobody has it, skip (can't
    clone from nothing).

    Returns {(owner, repo): "exists"|"created"|"error: ..."|"..."}.
    """
    accounts = get_accounts()
    if not accounts:
        log.warning("no accounts configured, skipping bootstrap check")
        return {}

    # Phase 1: check who has / doesn't have the repo
    status = {}
    source = None
    for pat, owner, repo in accounts:
        key = (owner, repo)
        try:
            if repo_exists(pat, owner, repo):
                status[key] = "exists"
                if source is None:
                    source = (pat, owner, repo)
            else:
                status[key] = "missing"
        except Exception as e:
            log.error("check %s/%s failed: %s", owner, repo, e)
            status[key] = f"check_error: {e}"

    if source is None:
        log.error("no account has a bootstrap repo — cannot clone from nothing; "
                  "create one manually first")
        return status

    # Phase 2: create missing repos
    for pat, owner, repo in accounts:
        key = (owner, repo)
        if status.get(key) != "missing":
            continue
        try:
            result = ensure_bootstrap_repo(
                pat, owner, repo,
                source[0], source[1], source[2],
                private=private,
            )
            status[key] = result
        except Exception as e:
            log.error("ensure %s/%s failed: %s", owner, repo, e)
            status[key] = f"error: {e}"

    return status


# --------------------------------------------------------------------------- #
# GitHub-side state machine helpers (RUN_LIFECYCLE.md §2-§5)                  #
# --------------------------------------------------------------------------- #

def gh_list_active_runs(per_page: int = 30) -> list[dict]:
    """List runs with status != 'completed' (queued + in_progress).

    GitHub API's status param doesn't support multi-value, so we fetch without
    status filter and filter locally. One request covers both states.
    """
    runs = []
    for pat, owner, repo in get_accounts():
        try:
            data = gh_request("GET", f"/repos/{owner}/{repo}/actions/runs?per_page={per_page}")
            for r in data.get("workflow_runs", []):
                if r.get("status") != "completed":
                    r["_owner"] = owner
                    r["_repo"] = repo
                    r["_pat"] = pat
                    runs.append(r)
        except Exception as e:
            log.warning("gh_list_active_runs: %s/%s failed: %s", owner, repo, e)
            continue
    return runs


def gh_find_run_by_nonce(nonce: str, template: str, per_page: int = 10) -> dict | None:
    """Find a run whose display_title contains the nonce.

    bootstrap.yml uses run-name: "${{ inputs.template }}-${{ inputs.nonce }}"
    so display_title will be like "atlas1-abc123".
    """
    for pat, owner, repo in get_accounts():
        try:
            data = gh_request("GET", f"/repos/{owner}/{repo}/actions/runs?per_page={per_page}")
            for r in data.get("workflow_runs", []):
                title = r.get("display_title") or r.get("name") or ""
                if nonce in title:
                    r["_owner"] = owner
                    r["_repo"] = repo
                    r["_pat"] = pat
                    return r
        except Exception as e:
            log.warning("gh_find_run_by_nonce: %s/%s failed: %s", owner, repo, e)
            continue
    return None


def gh_get_run(run_id: int) -> dict | None:
    """Get single run details across all accounts."""
    for pat, owner, repo in get_accounts():
        try:
            data = gh_request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")
            if data:
                data["_owner"] = owner
                data["_repo"] = repo
                data["_pat"] = pat
                return data
        except Exception as e:
            if "404" not in str(e):
                log.warning("gh_get_run: %s/%s failed: %s", owner, repo, e)
            continue
    return None


def gh_cancel_run(run_id: int) -> bool:
    """Cancel a GitHub Actions run. Returns True on success."""
    for pat, owner, repo in get_accounts():
        try:
            gh_request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")
            log.info("gh_cancel_run: cancelled %d on %s/%s", run_id, owner, repo)
            return True
        except Exception as e:
            if "404" in str(e) or "409" in str(e):
                continue
            log.warning("gh_cancel_run: %s/%s failed: %s", owner, repo, e)
            continue
    return False


def gh_dispatch_workflow(template: str, nonce: str, ref: str = "main") -> bool:
    """Dispatch bootstrap workflow with template + nonce inputs."""
    for pat, owner, repo in get_accounts():
        try:
            gh_request(
                "POST",
                f"/repos/{owner}/{repo}/actions/workflows/bootstrap.yml/dispatches",
                data={"ref": ref, "inputs": {"template": template, "nonce": nonce}},
            )
            log.info("gh_dispatch_workflow: dispatched on %s/%s (template=%s nonce=%s)",
                     owner, repo, template, nonce)
            return True
        except Exception as e:
            log.warning("gh_dispatch_workflow: %s/%s failed: %s", owner, repo, e)
            continue
    return False


# --------------------------------------------------------------------------- #
# GitHub-side state machine helpers (RUN_LIFECYCLE.md §2-§5)                  #
# --------------------------------------------------------------------------- #

def gh_list_active_runs(per_page: int = 30) -> list[dict]:
    """List runs with status != 'completed' (queued + in_progress).

    GitHub API's status param doesn't support multi-value, so we fetch without
    status filter and filter locally. One request covers both states.
    """
    runs = []
    for pat, owner, repo in get_accounts():
        try:
            data = gh_request("GET", f"/repos/{owner}/{repo}/actions/runs?per_page={per_page}")
            for r in data.get("workflow_runs", []):
                if r.get("status") != "completed":
                    r["_owner"] = owner
                    r["_repo"] = repo
                    r["_pat"] = pat
                    runs.append(r)
        except Exception as e:
            log.warning("gh_list_active_runs: %s/%s failed: %s", owner, repo, e)
            continue
    return runs


def gh_find_run_by_nonce(nonce: str, template: str, per_page: int = 10) -> dict | None:
    """Find a run whose display_title contains the nonce.

    bootstrap.yml uses run-name: "${{ inputs.template }}-${{ inputs.nonce }}"
    so display_title will be like "atlas1-abc123".
    """
    for pat, owner, repo in get_accounts():
        try:
            data = gh_request("GET", f"/repos/{owner}/{repo}/actions/runs?per_page={per_page}")
            for r in data.get("workflow_runs", []):
                title = r.get("display_title") or r.get("name") or ""
                if nonce in title:
                    r["_owner"] = owner
                    r["_repo"] = repo
                    r["_pat"] = pat
                    return r
        except Exception as e:
            log.warning("gh_find_run_by_nonce: %s/%s failed: %s", owner, repo, e)
            continue
    return None


def gh_get_run(run_id: int) -> dict | None:
    """Get single run details across all accounts."""
    for pat, owner, repo in get_accounts():
        try:
            data = gh_request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")
            if data:
                data["_owner"] = owner
                data["_repo"] = repo
                data["_pat"] = pat
                return data
        except Exception as e:
            if "404" not in str(e):
                log.warning("gh_get_run: %s/%s failed: %s", owner, repo, e)
            continue
    return None


def gh_cancel_run(run_id: int) -> bool:
    """Cancel a GitHub Actions run. Returns True on success."""
    for pat, owner, repo in get_accounts():
        try:
            gh_request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")
            log.info("gh_cancel_run: cancelled %d on %s/%s", run_id, owner, repo)
            return True
        except Exception as e:
            if "404" in str(e) or "409" in str(e):
                continue
            log.warning("gh_cancel_run: %s/%s failed: %s", owner, repo, e)
            continue
    return False


def gh_dispatch_workflow(template: str, nonce: str, ref: str = "main") -> bool:
    """Dispatch bootstrap workflow with template + nonce inputs."""
    for pat, owner, repo in get_accounts():
        try:
            gh_request(
                "POST",
                f"/repos/{owner}/{repo}/actions/workflows/bootstrap.yml/dispatches",
                data={"ref": ref, "inputs": {"template": template, "nonce": nonce}},
            )
            log.info("gh_dispatch_workflow: dispatched on %s/%s (template=%s nonce=%s)",
                     owner, repo, template, nonce)
            return True
        except Exception as e:
            log.warning("gh_dispatch_workflow: %s/%s failed: %s", owner, repo, e)
            continue
    return False
