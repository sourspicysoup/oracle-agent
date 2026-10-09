#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cf_ip_rotate.py — Clash YAML Host 域名被 GFW 墙时自动换 Host（server 不动）

三层检测：
  Layer1: HTTP 400 + sec-websocket-version:13 → Runner/隧道正常
  Layer2: WS 101 → Cloudflare 网络正常
  Layer3: 中国 CDN IP + Host 头探测 → GFW 双向审查

L3 remediation: find_new_subdomain → create_dns → add_ingress → rewrite_yaml → push → verify
L2 remediation: az.yml Host A→B（检测 B WS OK 后切换）
L1 remediation: 调 Oracle Agent 重建 Runner

用法:
  python3 cf_ip_rotate.py --check-only      # 只检测，不做 remediation
  python3 cf_ip_rotate.py --dry-run         # 检测 + 预览 remediation（不建 DNS/不推送）
  python3 cf_ip_rotate.py                   # 检测 + remediation（生产模式）
  python3 cf_ip_rotate.py --force-host X    # 强制检测指定 Host
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# ────────────────────────── 常量 ──────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
HISTORY_FILE = SCRIPT_DIR / "rotate_history.json"
LOG_FILE = SCRIPT_DIR / "rotate.log"

CF_API_BASE = "https://api.cloudflare.com/client/v4"
GITHUB_API_BASE = "https://api.github.com"

# Layer3 GFW 检测常量（复用 GFWcheck 逻辑）
CDN_DOMAINS = ["m.10099.com.cn", "app.10099.com.cn", "www.10099.com.cn"]
SENTINEL_HOST = "www.epicgames.com"
L3_PORT = 80
L3_TIMEOUT = 5
L3_THREADS = 10

# Layer1/2 检测常量
L1_TIMEOUT = 10
L2_TIMEOUT = 10
WS_PORT = 2095  # az.yml 里 proxy 的 port

UA = "cf-ip-rotate/1.0"

# ────────────────────────── 日志 ──────────────────────────

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ────────────────────────── 配置 ──────────────────────────

def load_env():
    """读 .env，返回 dict。缺失关键 key 时报错退出。"""
    if not ENV_FILE.exists():
        log(f".env not found at {ENV_FILE}", "ERROR")
        sys.exit(1)

    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

    required = [
        "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_ZONE_NAME",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_TUNNEL_UUID",
        "CLOUDFLARE_API_TOKEN_BACKUP", "CLOUDFLARE_ZONE_ID_BACKUP",
        "CLOUDFLARE_ZONE_NAME_BACKUP", "CLOUDFLARE_ACCOUNT_ID_BACKUP",
        "CLOUDFLARE_TUNNEL_UUID_BACKUP",
        "GITHUB_PAT_FILE", "GITHUB_REPO", "GITHUB_FILE", "GITHUB_BRANCH",
        "CHECK_HOST",
    ]
    missing = [k for k in required if not env.get(k)]
    if missing:
        log(f"Missing required env keys: {missing}", "ERROR")
        sys.exit(1)

    return env


def load_pat(env):
    """读 GitHub PAT 文件。"""
    pat_file = Path(env["GITHUB_PAT_FILE"])
    if not pat_file.exists():
        log(f"PAT file not found: {pat_file}", "ERROR")
        sys.exit(1)
    return pat_file.read_text().strip()


def get_active_tunnel_config(env, current_host):
    """根据当前 Host 判断用的是哪个 tunnel，返回该 tunnel 的 CF 配置 dict。"""
    zone_a = env["CLOUDFLARE_ZONE_NAME"]
    zone_b = env["CLOUDFLARE_ZONE_NAME_BACKUP"]

    if zone_b in current_host:
        return {
            "token": env["CLOUDFLARE_API_TOKEN_BACKUP"],
            "zone_id": env["CLOUDFLARE_ZONE_ID_BACKUP"],
            "zone_name": zone_b,
            "account_id": env["CLOUDFLARE_ACCOUNT_ID_BACKUP"],
            "tunnel_uuid": env["CLOUDFLARE_TUNNEL_UUID_BACKUP"],
            "role": "backup",
        }
    return {
        "token": env["CLOUDFLARE_API_TOKEN"],
        "zone_id": env["CLOUDFLARE_ZONE_ID"],
        "zone_name": zone_a,
        "account_id": env["CLOUDFLARE_ACCOUNT_ID"],
        "tunnel_uuid": env["CLOUDFLARE_TUNNEL_UUID"],
        "role": "primary",
    }


def get_backup_tunnel_config(env, active_cfg):
    """返回 active tunnel 的备用 tunnel 配置。"""
    prefix = env.get("CLOUDFLARE_NEW_SUBDOMAIN_PREFIX", "opencode-api")
    if active_cfg["role"] == "primary":
        return {
            "token": env["CLOUDFLARE_API_TOKEN_BACKUP"],
            "zone_id": env["CLOUDFLARE_ZONE_ID_BACKUP"],
            "zone_name": env["CLOUDFLARE_ZONE_NAME_BACKUP"],
            "account_id": env["CLOUDFLARE_ACCOUNT_ID_BACKUP"],
            "tunnel_uuid": env["CLOUDFLARE_TUNNEL_UUID_BACKUP"],
            "host": f"{prefix}.{env['CLOUDFLARE_ZONE_NAME_BACKUP']}",
        }
    return {
        "token": env["CLOUDFLARE_API_TOKEN"],
        "zone_id": env["CLOUDFLARE_ZONE_ID"],
        "zone_name": env["CLOUDFLARE_ZONE_NAME"],
        "account_id": env["CLOUDFLARE_ACCOUNT_ID"],
        "tunnel_uuid": env["CLOUDFLARE_TUNNEL_UUID"],
        "host": f"{prefix}.{env['CLOUDFLARE_ZONE_NAME']}",
    }

# ────────────────────────── Layer1: HTTP 检测 ──────────────────────────

def layer1_http_check(host: str) -> tuple:
    """HTTP 80 端口明文检测。判据：状态码==400 + sec-websocket-version:13。

    Returns: (ok: bool, detail: str)
    """
    try:
        s = socket.create_connection((host, 80), timeout=L1_TIMEOUT)
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(req.encode())
        data = b""
        while len(data) < 512:
            chunk = s.recv(512 - len(data))
            if not chunk:
                break
            data += chunk
        s.close()

        if not data:
            return False, "no_response"

        first_line = data.split(b"\r\n")[0].decode("ascii", errors="replace")
        parts = first_line.split()
        if len(parts) < 2:
            return False, f"malformed: {first_line}"
        status_code = int(parts[1])

        has_ws_version = b"sec-websocket-version" in data.lower()

        if status_code == 400 and has_ws_version:
            return True, "400+ws-version:13"
        return False, f"status={status_code} ws_ver={has_ws_version}"

    except (ConnectionRefusedError, ConnectionResetError) as e:
        return False, f"tcp_rst: {type(e).__name__}"
    except socket.timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"error: {e}"


# ────────────────────────── Layer2: WS 检测 ──────────────────────────

def layer2_ws_check(host: str) -> tuple:
    """WebSocket 握手检测。判据：收到 101 Switching Protocols。

    连 2095 端口（az.yml proxy port），发 WS Upgrade 握手。
    """
    try:
        s = socket.create_connection((host, WS_PORT), timeout=L2_TIMEOUT)
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {UA}\r\n\r\n"
        )
        s.sendall(req.encode())
        data = b""
        while len(data) < 256:
            chunk = s.recv(256 - len(data))
            if not chunk:
                break
            data += chunk
        s.close()

        if not data:
            return False, "no_response"

        first_line = data.split(b"\r\n")[0].decode("ascii", errors="replace")
        if "101" in first_line:
            return True, "101 Switching Protocols"
        return False, first_line

    except (ConnectionRefusedError, ConnectionResetError) as e:
        return False, f"tcp_rst: {type(e).__name__}"
    except socket.timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"error: {e}"


# ────────────────────────── Layer3: GFW 检测 ──────────────────────────

def _http_tcp_test(host: str, ip: str, timeout: float) -> str:
    """裸 socket 连 80 端口发 HTTP/1.1 明文请求，返回状态标签。"""
    try:
        s = socket.create_connection((ip, L3_PORT), timeout=timeout)
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(req.encode())
        data = s.recv(128)
        s.close()
        if not data:
            return "no_response"
        if b"HTTP/" in data:
            return "success"
        return "unknown"
    except (ConnectionRefusedError, ConnectionResetError):
        return "tcp_rst"
    except socket.timeout:
        return "timeout"
    except Exception:
        return "error"


def _collect_cdn_ips() -> list:
    """DNS 解析中国 CDN 域名收集 IP。"""
    ips = set()
    for d in CDN_DOMAINS:
        try:
            for info in socket.getaddrinfo(d, None, socket.AF_INET):
                ips.add(info[4][0])
        except socket.gaierror:
            pass
    return sorted(ips)


def _prefilter_ips(ips: list) -> list:
    """sentinel 预检：用 www.epicgames.com 做 Host 头探活，只保留干净 IP。"""
    def _probe(ip):
        return ip, _http_tcp_test(SENTINEL_HOST, ip, L3_TIMEOUT) == "success"

    good = []
    with ThreadPoolExecutor(max_workers=L3_THREADS) as ex:
        for ip, ok in ex.map(_probe, ips):
            if ok:
                good.append(ip)
    return good


def layer3_host_blocked_check(host: str) -> tuple:
    """GFW 双向审查检测。

    ① 收集中国 CDN IP
    ② sentinel 预检得干净 IP
    ③ 干净 IP 上带目标 Host 发 GET /
    ④ 任一收到 HTTP 状态码 → 未墙；全部 RST/超时 → blocked

    Returns: (blocked: bool, detail: str)
    """
    ips = _collect_cdn_ips()
    if not ips:
        return False, "no_cdn_ips: cannot determine"

    log(f"Layer3: collected {len(ips)} CDN IPs: {ips}")
    clean_ips = _prefilter_ips(ips)
    log(f"Layer3: {len(clean_ips)} clean IPs after sentinel: {clean_ips}")
    if not clean_ips:
        return False, "no_clean_ips: sentinel all failed"

    def _test(ip):
        return _http_tcp_test(host, ip, L3_TIMEOUT)

    results = []
    with ThreadPoolExecutor(max_workers=L3_THREADS) as ex:
        results = list(ex.map(_test, clean_ips))

    success_count = results.count("success")
    total = len(results)

    if success_count > 0:
        return False, f"not_blocked: {success_count}/{total} IPs responded"
    return True, f"blocked: 0/{total} responded, all RST/timeout"

# ────────────────────────── diagnose ──────────────────────────

def diagnose(host: str) -> str:
    """按序 Layer1→Layer2→Layer3，返回状态字符串。

    Returns: healthy / runner_down / cf_ws_blocked / host_blocked / inconclusive
    """
    l1_ok, l1_detail = layer1_http_check(host)
    log(f"Layer1 HTTP: ok={l1_ok} detail={l1_detail}")
    if not l1_ok:
        if "timeout" in l1_detail or "no_response" in l1_detail:
            return "inconclusive"
        if "status=403" in l1_detail:
            log("L1: 403 from CF, treating as tunnel issue")
            return "cf_ws_blocked"
        return "runner_down"

    l2_ok, l2_detail = layer2_ws_check(host)
    log(f"Layer2 WS: ok={l2_ok} detail={l2_detail}")
    if not l2_ok:
        return "cf_ws_blocked"

    l3_blocked, l3_detail = layer3_host_blocked_check(host)
    log(f"Layer3 GFW: blocked={l3_blocked} detail={l3_detail}")
    if l3_blocked:
        return "host_blocked"

    return "healthy"


# ────────────────────────── CF API ──────────────────────────

def cf_api(method: str, path: str, body=None, token: str = None):
    """CF API 封装。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{CF_API_BASE}{path}"
    resp = requests.request(method, url, headers=headers, json=body, timeout=15)
    data = resp.json()
    if not data.get("success", False):
        log(f"CF API error: {method} {path} -> {data.get('errors')}", "ERROR")
    return data


def list_dns_records(zone_id: str, token: str, name: str = None):
    """查 DNS 记录。"""
    path = f"/zones/{zone_id}/dns_records"
    if name:
        path += f"?name={name}"
    data = cf_api("GET", path, token=token)
    return data.get("result", [])


def list_tunnel_ingress_hostnames(account_id: str, tunnel_id: str, token: str) -> set:
    """查 CF Tunnel configurations，返回 ingress 中已配置的 hostname 集合。"""
    path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
    data = cf_api("GET", path, token=token)
    ingress = data.get("result", {}).get("config", {}).get("ingress", [])
    return {rule.get("hostname") for rule in ingress if rule.get("hostname")}


def gfw_host_precheck(host: str) -> bool:
    """GFW 预检：候选 Host 在无 DNS 状态下用 clean CDN IP + Host header 探测。

    候选 Host 此时尚无 DNS 记录，利用 L3 的 "指定 IP + Host header" 机制：
    向 clean CDN IP 发 HTTP 请求，Host 头设为候选域名。
    有 HTTP 响应 → 候选 Host 可用；全部 RST/timeout → 疑似被墙。

    Returns: True = 可用（至少 1 个 IP 收到 HTTP 响应），False = 疑似被墙
    """
    ips = _collect_cdn_ips()
    if not ips:
        log(f"GFW precheck: no CDN IPs, assuming pass", "WARN")
        return True

    clean_ips = _prefilter_ips(ips)
    if not clean_ips:
        log(f"GFW precheck: no clean IPs after sentinel, assuming pass", "WARN")
        return True

    def _test(ip):
        return _http_tcp_test(host, ip, L3_TIMEOUT)

    results = []
    with ThreadPoolExecutor(max_workers=L3_THREADS) as ex:
        results = list(ex.map(_test, clean_ips))

    success_count = results.count("success")
    total = len(results)
    log(f"GFW precheck {host}: {success_count}/{total} IPs responded")

    return success_count > 0


def find_new_subdomain(active: dict, prefix: str) -> str:
    """递增 opencode-api-N，取第一个未占用且 GFW 未墙者。

    占用判断 = CF DNS 记录 + CF Tunnel ingress routes（source of truth = Cloudflare）。
    rotate_history.json 仅用于审计，不参与可用性判定。
    """
    # 查 CF Tunnel ingress 已配置的 hostname
    tunnel_hostnames = list_tunnel_ingress_hostnames(
        active["account_id"], active["tunnel_uuid"], active["token"]
    )
    log(f"find_new_subdomain: tunnel has {len(tunnel_hostnames)} ingress hostnames")

    for n in range(2, 101):
        candidate = f"{prefix}-{n}.{active['zone_name']}"

        # ① CF DNS 记录检查
        records = list_dns_records(active["zone_id"], active["token"], name=candidate)
        if records:
            log(f"find_new_subdomain: {candidate} exists in DNS, skip")
            continue

        # ② CF Tunnel ingress 检查
        if candidate in tunnel_hostnames:
            log(f"find_new_subdomain: {candidate} exists in tunnel ingress, skip")
            continue

        # ③ GFW 预检：候选 Host 本身是否已被网络侧拦截
        if not gfw_host_precheck(candidate):
            log(f"find_new_subdomain: {candidate} GFW precheck FAILED, skip")
            continue

        log(f"find_new_subdomain: {candidate} passed all checks (DNS + tunnel + GFW)")
        return candidate

    log("find_new_subdomain: exceeded 100 attempts", "ERROR")
    return None


def create_dns_record(zone_id: str, token: str, name: str, tunnel_uuid: str):
    """建 CNAME -> {tunnel_uuid}.cfargotunnel.com（proxied=true, ttl=1）。"""
    body = {
        "type": "CNAME",
        "name": name,
        "content": f"{tunnel_uuid}.cfargotunnel.com",
        "proxied": True,
        "ttl": 1,
    }
    data = cf_api("POST", f"/zones/{zone_id}/dns_records", body=body, token=token)
    return data.get("result", {})


def add_tunnel_ingress(account_id: str, tunnel_id: str, hostname: str, token: str):
    """PUT configurations，把新 hostname 插入 ingress -> http://localhost:11451。"""
    path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
    data = cf_api("GET", path, token=token)
    config = data.get("result", {}).get("config", {})
    ingress = config.get("ingress", [])

    for rule in ingress:
        if rule.get("hostname") == hostname:
            log(f"Ingress rule for {hostname} already exists, skipping")
            return data

    new_rule = {"hostname": hostname, "service": "http://localhost:11451"}

    new_ingress = []
    for rule in ingress:
        is_catchall = "hostname" not in rule or rule.get("hostname") is None
        if is_catchall:
            new_ingress.append(new_rule)
        new_ingress.append(rule)

    body = {"config": {"ingress": new_ingress}}
    return cf_api("PUT", path, body=body, token=token)

# ────────────────────────── YAML 重写 + 校验 ──────────────────────────

def extract_proxies(text: str) -> list:
    """解析 YAML，返回 proxies 列表。"""
    data = yaml.safe_load(text)
    return data.get("proxies", []) if data else []


def _proxy_server_hash(proxies: list) -> str:
    """计算所有 proxy 的 server 字段 hash（用于校验 server 不变）。"""
    servers = [p.get("server", "") for p in proxies]
    return hashlib.md5(json.dumps(servers, sort_keys=True).encode()).hexdigest()


def _proxy_field_set(proxies: list) -> set:
    """提取所有 proxy 的关键字段（port/uuid/tls/network/path），用于校验不变。"""
    fields = set()
    for p in proxies:
        fields.add(("port", p.get("port")))
        fields.add(("uuid", p.get("uuid")))
        fields.add(("tls", p.get("tls")))
        fields.add(("network", p.get("network")))
        ws = p.get("ws-opts", {})
        fields.add(("path", ws.get("path")))
    return fields


def rewrite_yaml(text: str, new_host: str) -> str:
    """纯文本替换 Host 字段 + 结构校验。

    校验：
    - parse before YAML → 确认 proxies==19
    - 记录 before_server_hash + before_field_set
    - 精确文本替换 Host
    - parse after YAML → 确认 proxies==19
    - 确认 19/19 Host==new_host
    - 确认 after_server_hash==before_server_hash
    - 确认 port/uuid/tls/network/path 不变

    Raises ValueError if validation fails.
    """
    # before
    before_proxies = extract_proxies(text)
    if len(before_proxies) != 19:
        raise ValueError(f"Expected 19 proxies, got {len(before_proxies)}")
    before_hash = _proxy_server_hash(before_proxies)
    before_fields = _proxy_field_set(before_proxies)

    # 找当前 Host
    before_hosts = set()
    for p in before_proxies:
        h = p.get("ws-opts", {}).get("headers", {}).get("Host", "")
        before_hosts.add(h)
    if len(before_hosts) != 1:
        raise ValueError(f"Expected 1 unique Host, got {before_hosts}")
    old_host = before_hosts.pop()
    log(f"rewrite_yaml: {old_host} -> {new_host}")

    # 精确文本替换
    new_text = text.replace(old_host, new_host)

    # after
    after_proxies = extract_proxies(new_text)
    if len(after_proxies) != 19:
        raise ValueError(f"After: expected 19 proxies, got {len(after_proxies)}")

    after_hash = _proxy_server_hash(after_proxies)
    if after_hash != before_hash:
        raise ValueError(f"server hash changed: {before_hash} -> {after_hash}")

    after_fields = _proxy_field_set(after_proxies)
    if after_fields != before_fields:
        raise ValueError(f"fields changed: {before_fields} != {after_fields}")

    after_hosts = set()
    for p in after_proxies:
        h = p.get("ws-opts", {}).get("headers", {}).get("Host", "")
        after_hosts.add(h)
    if after_hosts != {new_host}:
        raise ValueError(f"After: expected all Host=={new_host}, got {after_hosts}")

    log("rewrite_yaml: validation passed (19/19 Host changed, server/port/uuid/tls/network/path unchanged)")
    return new_text


# ────────────────────────── GitHub ──────────────────────────

def fetch_yaml_raw(env, pat):
    """从 GitHub raw 拉取 az.yml，返回 (content, sha)。"""
    repo = env["GITHUB_REPO"]
    file_path = env["GITHUB_FILE"]
    branch = env["GITHUB_BRANCH"]

    # 先通过 API 获取 sha
    api_url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{file_path}?ref={branch}"
    resp = requests.get(api_url, headers={"Authorization": f"token {pat}"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    sha = data["sha"]
    content = base64.b64decode(data["content"]).decode()
    return content, sha


def push_to_github(env, pat, content, sha):
    """Contents API PUT，committer=GitHub bot。"""
    repo = env["GITHUB_REPO"]
    file_path = env["GITHUB_FILE"]
    branch = env["GITHUB_BRANCH"]

    url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{file_path}"
    body = {
        "message": f"chore: rotate Host ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        "content": base64.b64encode(content.encode()).decode(),
        "sha": sha,
        "branch": branch,
        "committer": {
            "name": env.get("GITHUB_COMMITTER_NAME", "github-actions[bot]"),
            "email": env.get("GITHUB_COMMITTER_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com"),
        },
    }
    resp = requests.put(url, json=body, headers={"Authorization": f"token {pat}"}, timeout=15)
    if resp.status_code in (200, 201):
        log(f"Pushed to GitHub: {repo}/{file_path}")
        return resp.json()
    log(f"GitHub push failed: {resp.status_code} {resp.text}", "ERROR")
    return None

# ────────────────────────── verify ──────────────────────────

def verify(host: str) -> bool:
    """对新 Host 重跑 Layer1/Layer2/Layer3。"""
    log(f"Verifying {host}...")
    status = diagnose(host)
    if status == "healthy":
        log(f"Verify PASSED: {host} is healthy")
        return True
    log(f"Verify FAILED: {host} status={status}", "WARN")
    return False


# ────────────────────────── rotate_history ──────────────────────────

def record_history(old_host, new_host, reason, tunnel_role):
    """记录切换历史。"""
    history = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())
    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "old_host": old_host,
        "new_host": new_host,
        "reason": reason,
        "tunnel_role": tunnel_role,
    })
    HISTORY_FILE.write_text(json.dumps(history, indent=2))
    log(f"Recorded history: {old_host} -> {new_host} ({reason})")


# ────────────────────────── L3 remediation ──────────────────────────

def l3_remediation(env, current_host, dry_run=False):
    """L3: Host 被墙 → 在 active tunnel 的 zone 创建新子域名（CF DNS + Tunnel routes + GFW precheck）→ 改 az.yml → 推 GitHub。"""
    active = get_active_tunnel_config(env, current_host)
    log(f"L3: active tunnel = {active['role']} ({active['zone_name']})")

    # 1. find new subdomain
    prefix = env.get("CLOUDFLARE_NEW_SUBDOMAIN_PREFIX", "opencode-api")
    new_host = find_new_subdomain(active, prefix)
    if not new_host:
        log("L3: failed to find new subdomain", "ERROR")
        return False
    log(f"L3: new subdomain = {new_host}")

    if dry_run:
        log("L3: --dry-run, skipping DNS/ingress/push")
        # 仍然预览 YAML diff
        content, sha = fetch_yaml_raw(env, load_pat(env))
        try:
            new_content = rewrite_yaml(content, new_host)
            log(f"L3 dry-run: YAML rewrite OK, {len(content)} -> {len(new_content)} bytes")
        except ValueError as e:
            log(f"L3 dry-run: YAML rewrite FAILED: {e}", "ERROR")
        return True

    # 2. create DNS CNAME
    log(f"L3: creating DNS CNAME {new_host} -> {active['tunnel_uuid']}.cfargotunnel.com")
    dns_result = create_dns_record(active["zone_id"], active["token"], new_host, active["tunnel_uuid"])
    if not dns_result:
        log("L3: DNS creation failed", "ERROR")
        return False
    log(f"L3: DNS created: {dns_result.get('name')}")

    # 3. add tunnel ingress
    log(f"L3: adding tunnel ingress for {new_host}")
    add_tunnel_ingress(active["account_id"], active["tunnel_uuid"], new_host, active["token"])

    # 4. fetch az.yml
    pat = load_pat(env)
    content, sha = fetch_yaml_raw(env, pat)
    log(f"L3: fetched az.yml ({len(content)} bytes, sha={sha[:8]}...)")

    # 5. rewrite YAML
    try:
        new_content = rewrite_yaml(content, new_host)
    except ValueError as e:
        log(f"L3: YAML rewrite failed: {e}", "ERROR")
        return False

    # 6. push to GitHub
    push_result = push_to_github(env, pat, new_content, sha)
    if not push_result:
        log("L3: GitHub push failed", "ERROR")
        return False

    # 7. verify
    time.sleep(5)  # 等 DNS 传播
    if not verify(new_host):
        log("L3: verify failed, but changes already pushed", "WARN")
        # 不回滚——新 Host 可能需要更长时间传播
    else:
        # 8. record history
        record_history(current_host, new_host, "host_blocked", active["role"])

    # 9. update .env CHECK_HOST
    update_env_check_host(new_host)
    log(f"L3: remediation complete. New CHECK_HOST = {new_host}")
    return True


# ────────────────────────── L2 remediation ──────────────────────────

def l2_remediation(env, current_host, dry_run=False):
    """L2: current tunnel WS 故障 → 切到 backup tunnel。

    ① 检测 backup tunnel WS
    ② rewrite az.yml Host: current -> backup
    ③ push GitHub
    ④ verify backup
    ⑤ verify OK → swap(current, backup)
    ⑥ verify FAIL → 检查 current 是否恢复 → rollback or 告警
    """
    active = get_active_tunnel_config(env, current_host)
    backup = get_backup_tunnel_config(env, active)
    backup_host = backup["host"]
    log(f"L2: active={active['role']} backup_host={backup_host}")

    # ① 检测 backup WS
    l2_ok, l2_detail = layer2_ws_check(backup_host)
    log(f"L2: backup WS check: ok={l2_ok} detail={l2_detail}")
    if not l2_ok:
        log("L2: backup tunnel also failed, cannot failover", "ERROR")
        return False

    if dry_run:
        log("L2: --dry-run, skipping push")
        return True

    # ② fetch + rewrite
    pat = load_pat(env)
    content, sha = fetch_yaml_raw(env, pat)
    try:
        new_content = rewrite_yaml(content, backup_host)
    except ValueError as e:
        log(f"L2: YAML rewrite failed: {e}", "ERROR")
        return False

    # ③ push
    push_result = push_to_github(env, pat, new_content, sha)
    if not push_result:
        log("L2: GitHub push failed", "ERROR")
        return False

    # ④ verify backup
    time.sleep(5)
    if verify(backup_host):
        log("L2: backup verified OK, failover complete")
        record_history(current_host, backup_host, "cf_ws_blocked", backup["tunnel_uuid"])
        update_env_check_host(backup_host)
        return True

    # ⑤ verify FAIL → 检查原 current
    log("L2: backup verify FAILED, checking if original recovered...", "WARN")
    l1_ok, _ = layer1_http_check(current_host)
    l2_ok, _ = layer2_ws_check(current_host)
    if l1_ok and l2_ok:
        log("L2: original recovered, rolling back")
        # rollback: push original content back
        push_to_github(env, pat, content, push_result["content"]["sha"])
        return False
    else:
        log("L2: both tunnels failed, alerting", "ERROR")
        return False


# ────────────────────────── L1 remediation ──────────────────────────

def l1_remediation(env, dry_run=False):
    """L1: Runner 故障 → 调 Oracle Agent 重建 Runner。

    需要ORACLE_ADMIN_TOKEN。如果未配置，只告警不执行。
    """
    admin_token = env.get("ORACLE_ADMIN_TOKEN", "")
    api_url = env.get("ORACLE_AGENT_API_URL", "http://127.0.0.1:8700")
    template = env.get("ORACLE_TEMPLATE_NAME", "atlas1")

    if not admin_token:
        log("L1: ORACLE_ADMIN_TOKEN not configured, cannot rebuild Runner", "ERROR")
        return False

    if dry_run:
        log("L1: --dry-run, would call Oracle Agent to rebuild Runner")
        return True

    log(f"L1: triggering Oracle Agent rebuild with template {template}")
    # POST /api/v1/admin/trigger
    try:
        resp = requests.post(
            f"{api_url}/api/v1/admin/trigger",
            json={"count": 1},
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            log("L1: Oracle Agent trigger OK")
            return True
        log(f"L1: Oracle Agent trigger failed: {resp.status_code} {resp.text}", "ERROR")
        return False
    except Exception as e:
        log(f"L1: Oracle Agent error: {e}", "ERROR")
        return False


# ────────────────────────── .env 更新 ──────────────────────────

def update_env_check_host(new_host):
    """更新 .env 中的 CHECK_HOST。"""
    if not ENV_FILE.exists():
        return
    lines = ENV_FILE.read_text().splitlines()
    updated = []
    for line in lines:
        if line.startswith("CHECK_HOST="):
            updated.append(f"CHECK_HOST={new_host}")
        else:
            updated.append(line)
    ENV_FILE.write_text("\n".join(updated) + "\n")

# ────────────────────────── main ──────────────────────────

def main():
    parser = argparse.ArgumentParser(description="cf_ip_rotate: auto-rotate Clash YAML Host on GFW block")
    parser.add_argument("--check-only", action="store_true", help="Only run detection, no remediation")
    parser.add_argument("--dry-run", action="store_true", help="Detection + preview remediation (no DNS/push)")
    parser.add_argument("--force-host", type=str, help="Force check a specific host instead of CHECK_HOST")
    args = parser.parse_args()

    env = load_env()
    check_host = args.force_host or env["CHECK_HOST"]

    log(f"=== cf_ip_rotate start (check_host={check_host}) ===")
    log(f"Mode: {'check-only' if args.check_only else 'dry-run' if args.dry_run else 'production'}")

    # 检测
    status = diagnose(check_host)
    log(f"diagnose result: {status}")

    if status == "healthy":
        log("All healthy, nothing to do.")
        return

    if status == "inconclusive":
        log("Inconclusive, not modifying az.yml. Will retry next cycle.")
        return

    if args.check_only:
        log("--check-only: skipping remediation")
        return

    # remediation
    if status == "runner_down":
        log("L1: runner_down detected, Oracle Agent persistent loop will handle")
    elif status == "cf_ws_blocked":
        log("L2: Tunnel WS blocked, attempting failover to backup...")
        l2_remediation(env, check_host, dry_run=args.dry_run)
    elif status == "host_blocked":
        log("L3: Host blocked by GFW, attempting rotation...")
        l3_remediation(env, check_host, dry_run=args.dry_run)

    log("=== cf_ip_rotate end ===")


if __name__ == "__main__":
    main()
