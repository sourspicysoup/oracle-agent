#!/usr/bin/env python3
"""Send a signed MIME message directly to recipient MX servers over SMTP/25.

Design:
- Oracle signs the message (DKIM) and stores the raw MIME.
- The runner downloads the MIME, extracts recipients from To/Cc/Bcc,
  resolves each recipient domain's MX records, and delivers on port 25.
- Bcc headers are stripped before delivery; Bcc addresses are still used
  in RCPT TO. Only the header section is modified, never the body.
- The script is stateless: Oracle owns retries, idempotency, and status.

Safety notes:
- Do not include "bcc" in the DKIM h= list, otherwise stripping Bcc breaks
  the signature.
- The script runs as root on the runner. Pin the source URL / verify its
  hash if the repository is not fully trusted.
"""

import argparse
import email.utils
import hashlib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from email.parser import BytesHeaderParser

MAX_BYTES_DEFAULT = 25 * 1024 * 1024
DEFAULT_TIMEOUT = 30
DIG_BIN_DEFAULT = "dig"

_CRLF = b"\r\n"
_HEADER_SEP = b"\r\n\r\n"
_BCC_HEADER_RE = re.compile(rb"^bcc:", re.IGNORECASE)
_HEADER_CONTINUATION_RE = re.compile(rb"^[ \t]")


class MXConnectionError(RuntimeError):
    """Connection/HELO/TLS-level failure; safe to try the next MX."""


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def download_mime(url, token, agent_id, path, size_limit):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Agent-Id": agent_id,
            "Accept": "message/rfc822",
            "User-Agent": "oracle-runner-mailer-dmx/2",
        },
    )
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=60) as resp:
        expected = (resp.headers.get("X-Email-SHA256") or "").strip().lower()
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > size_limit:
            raise RuntimeError("email exceeds size limit")
        with open(path, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > size_limit:
                    raise RuntimeError("email exceeds size limit")
                out.write(chunk)
                h.update(chunk)
    digest = h.hexdigest()
    verified = bool(expected)
    if expected and digest != expected:
        raise RuntimeError("email sha256 mismatch")
    return total, digest, verified


def split_headers_body(mime_bytes):
    """Return (header_bytes, body_bytes, has_separator)."""
    idx = mime_bytes.find(_HEADER_SEP)
    if idx < 0:
        return mime_bytes, b"", False
    return mime_bytes[:idx], mime_bytes[idx + len(_HEADER_SEP):], True


def read_meta(header_bytes):
    if b"dkim-signature:" not in header_bytes.lower():
        raise RuntimeError("missing DKIM-Signature header")
    msg = BytesHeaderParser().parsebytes(header_bytes)
    return msg.get("Message-ID")


def parse_recipients(header_bytes):
    """Extract deduplicated To/Cc/Bcc addresses from the header section."""
    msg = BytesHeaderParser().parsebytes(header_bytes)
    raw = []
    for field in ("To", "Cc", "Bcc"):
        raw.extend(msg.get_all(field) or [])
    seen = set()
    addrs = []
    for _name, addr in email.utils.getaddresses(raw):
        if not addr:
            continue
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        addrs.append(addr)
    return addrs


def strip_bcc_headers(mime_bytes):
    """Remove Bcc headers and folded continuations from the header section.

    The body is copied byte-for-byte. DKIM signatures normally exclude Bcc,
    so this keeps the signature valid.
    """
    header_bytes, body_bytes, has_sep = split_headers_body(mime_bytes)
    lines = header_bytes.split(_CRLF)
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _BCC_HEADER_RE.match(line):
            i += 1
            while i < len(lines) and _HEADER_CONTINUATION_RE.match(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    new_header = _CRLF.join(out)
    if has_sep:
        return new_header + _HEADER_SEP + body_bytes
    return new_header


def envelope_from_default(header_bytes, override):
    if override:
        return override
    msg = BytesHeaderParser().parsebytes(header_bytes)
    for field in ("Return-Path", "Resent-From", "From"):
        value = msg.get(field)
        if not value:
            continue
        addrs = email.utils.getaddresses((value,))
        if addrs and addrs[0][1]:
            return addrs[0][1]
    return ""


def domain_of(addr):
    if not addr or "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1].lower()


def dig_mx(domain, dig_bin, timeout):
    """Return (mx_list, error). mx_list = [(preference, host), ...] sorted."""
    cmd = [dig_bin, "+short", "MX", domain]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return [], f"dig binary not found: {dig_bin}"
    except subprocess.TimeoutExpired:
        return [], f"dig MX lookup timed out for {domain}"
    if proc.returncode != 0:
        return [], f"dig MX rc={proc.returncode} for {domain}: {proc.stderr.strip()}"

    mxs = []
    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pref = int(parts[0])
        except ValueError:
            pref = 0
        host = parts[1].rstrip(".")
        if host:
            mxs.append((pref, host))

    if mxs:
        mxs.sort(key=lambda x: (x[0], x[1]))
        return mxs, None

    # RFC 5321: if no MX record, use the domain's A/AAAA record.
    for rtype in ("A", "AAAA"):
        cmd = [dig_bin, "+short", rtype, domain]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            break
        if proc.returncode != 0:
            continue
        for line in proc.stdout.strip().splitlines():
            line = line.strip()
            if line:
                mxs.append((0, line))
        if mxs:
            mxs.sort(key=lambda x: (x[0], x[1]))
            return mxs, None

    return [], f"no MX or A/AAAA record for {domain}"


def _result(status, code=None, message=None, stage=None, temporary=None):
    out = {"status": status}
    if code is not None:
        out["code"] = code
    if message:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        out["message"] = str(message)
    if stage:
        out["stage"] = stage
    if temporary is not None:
        out["temporary"] = temporary
    return out


def _refused_map(refused):
    """Map lowercased address -> (original address, (code, message))."""
    out = {}
    for addr, value in (refused or {}).items():
        out[addr.lower()] = (addr, value)
    return out


def _smtp_code_temporary(code):
    return 400 <= code < 500


def send_to_mx(mx_host, addresses, envelope_from, mime_bytes, args):
    """Deliver one domain's recipients through one MX host.

    Returns {address: result_dict} for SMTP-level outcomes.
    Raises MXConnectionError for connection/HELO/TLS failures so the caller
    can try the next MX. SMTP-level 4xx/5xx responses are returned and do not
    trigger MX fallback (which could duplicate delivery or hide the real error).
    """
    if args.tls_verify:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl._create_unverified_context()

    client = smtplib.SMTP(
        mx_host, args.smtp_port, timeout=args.timeout, local_hostname=args.helo
    )
    try:
        code, resp = client.ehlo()
        if code != 250:
            code, resp = client.helo()
            if code != 250:
                raise MXConnectionError(f"HELO rejected by {mx_host}: {code} {resp}")

        has_starttls = client.has_extn("starttls")
        if args.tls in ("auto", "required") and has_starttls:
            try:
                client.starttls(context=ctx)
            except (OSError, ssl.SSLError, smtplib.SMTPException) as exc:
                raise MXConnectionError(f"STARTTLS failed for {mx_host}: {exc}") from exc
            code, resp = client.ehlo()
            if code != 250:
                raise MXConnectionError(
                    f"EHLO after STARTTLS rejected by {mx_host}: {code} {resp}"
                )
        elif args.tls == "required":
            raise MXConnectionError(f"STARTTLS required but not offered by {mx_host}")

        try:
            refused = client.sendmail(envelope_from, list(addresses), mime_bytes)
        except smtplib.SMTPRecipientsRefused as exc:
            refused = exc.recipients or {}
            lower = _refused_map(refused)
            results = {}
            for addr in addresses:
                hit = lower.get(addr.lower())
                if hit:
                    _orig, (code, resp) = hit
                    results[addr] = _result(
                        "refused", code=code, message=resp, stage="rcpt",
                        temporary=_smtp_code_temporary(code),
                    )
                else:
                    results[addr] = _result(
                        "refused", message="all recipients refused", stage="rcpt"
                    )
            return results
        except smtplib.SMTPSenderRefused as exc:
            code = exc.smtp_code
            resp = exc.smtp_error
            if isinstance(resp, bytes):
                resp = resp.decode("utf-8", "replace")
            return {
                addr: _result(
                    "failed", code=code, message=resp, stage="mail_from",
                    temporary=_smtp_code_temporary(code),
                )
                for addr in addresses
            }
        except smtplib.SMTPDataError as exc:
            code = exc.smtp_code
            resp = exc.smtp_error
            if isinstance(resp, bytes):
                resp = resp.decode("utf-8", "replace")
            return {
                addr: _result(
                    "failed", code=code, message=resp, stage="data",
                    temporary=_smtp_code_temporary(code),
                )
                for addr in addresses
            }
        except smtplib.SMTPHeloError as exc:
            raise MXConnectionError(f"HELO failed for {mx_host}: {exc}") from exc
        except smtplib.SMTPNotSupportedError as exc:
            return {
                addr: _result("failed", message=exc, stage="smtp", temporary=False)
                for addr in addresses
            }
        except smtplib.SMTPResponseException as exc:
            code = exc.smtp_code
            if code == 421:
                raise MXConnectionError(
                    f"{mx_host} closed connection: {code} {exc.smtp_error}"
                ) from exc
            return {
                addr: _result(
                    "failed", code=code, message=exc.smtp_error, stage="smtp",
                    temporary=_smtp_code_temporary(code),
                )
                for addr in addresses
            }
        except (OSError, ssl.SSLError) as exc:
            raise MXConnectionError(f"{mx_host}: {type(exc).__name__}: {exc}") from exc

        results = {}
        lower = _refused_map(refused)
        for addr in addresses:
            hit = lower.get(addr.lower())
            if hit:
                _orig, (code, resp) = hit
                results[addr] = _result(
                    "refused", code=code, message=resp, stage="rcpt",
                    temporary=_smtp_code_temporary(code),
                )
            else:
                results[addr] = _result("accepted", code=250)
        return results
    finally:
        try:
            client.quit()
        except Exception:
            client.close()


def _validate_address(addr, label):
    if not addr:
        raise RuntimeError(f"{label} address missing")
    if any(ch in addr for ch in ("\r", "\n", "<", ">")):
        raise RuntimeError(f"unsafe {label} address: {addr!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-url", help="Oracle server URL (overrides config)")
    parser.add_argument("--email-id", default="")
    parser.add_argument("--mime-file", default="", help="local MIME file for testing")
    parser.add_argument("--from", dest="envelope_from", default="")
    parser.add_argument("--smtp-port", type=int, default=25)
    parser.add_argument("--tls", choices=("auto", "required", "none"), default="auto")
    parser.add_argument(
        "--tls-verify",
        action="store_true",
        default=False,
        help="verify STARTTLS certificates (default: no, for MX compatibility)",
    )
    parser.add_argument("--helo", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--state-file", default="/opt/oracle-agent/state.json")
    parser.add_argument("--config-file", default="/opt/oracle-agent/config.json")
    parser.add_argument("--dig-bin", default=DIG_BIN_DEFAULT)
    parser.add_argument("--size-limit", type=int, default=MAX_BYTES_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="resolve MX only")
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="exit 1 if any recipient is not accepted",
    )
    args = parser.parse_args()

    path = None
    try:
        if args.mime_file:
            with open(args.mime_file, "rb") as f:
                mime = f.read()
            size = len(mime)
            sha = hashlib.sha256(mime).hexdigest()
            sha_verified = True
            if size > args.size_limit:
                raise RuntimeError("email exceeds size limit")
        else:
            if not args.email_id:
                raise RuntimeError("--email-id required unless --mime-file is used")
            state = load_json(args.state_file)
            token = state.get("token")
            agent_id = state.get("agent_id")
            if not token or not agent_id:
                raise RuntimeError("agent state missing token/agent_id")
            oracle_url = args.oracle_url or load_json(args.config_file).get("oracle_url")
            if not oracle_url:
                raise RuntimeError("oracle_url missing")

            fd, path = tempfile.mkstemp(prefix="signed-", suffix=".eml")
            os.close(fd)
            url = (
                oracle_url.rstrip("/")
                + f"/api/v1/email/{urllib.parse.quote(args.email_id, safe='')}/mime"
            )
            size, sha, sha_verified = download_mime(
                url, token, agent_id, path, args.size_limit
            )
            with open(path, "rb") as f:
                mime = f.read()

        if not mime.endswith(_CRLF):
            raise RuntimeError("MIME must end with CRLF")
        if b"\n" in mime.replace(_CRLF, b""):
            raise RuntimeError("MIME contains bare LF")

        header_bytes, _body_bytes, _has_sep = split_headers_body(mime)
        message_id = read_meta(header_bytes)
        envelope_from = envelope_from_default(header_bytes, args.envelope_from)
        _validate_address(envelope_from, "envelope From")
        if not args.helo:
            args.helo = domain_of(envelope_from) or envelope_from

        recipients = parse_recipients(header_bytes)
        if not recipients:
            raise RuntimeError("no recipients found in MIME headers")
        for addr in recipients:
            _validate_address(addr, "recipient")

        delivery_mime = strip_bcc_headers(mime)
        if not delivery_mime.endswith(_CRLF):
            delivery_mime += _CRLF

        domain_map = {}
        for addr in recipients:
            dom = domain_of(addr)
            if not dom:
                raise RuntimeError(f"bad recipient address: {addr}")
            domain_map.setdefault(dom, []).append(addr)

        all_results = []
        for dom, addrs in domain_map.items():
            mx_list, mx_err = dig_mx(dom, args.dig_bin, args.timeout)
            if not mx_list:
                for a in addrs:
                    all_results.append(
                        {
                            "address": a,
                            "domain": dom,
                            "status": "failed",
                            "error": mx_err or "no MX",
                        }
                    )
                continue

            if args.dry_run:
                pref, mx_host = mx_list[0]
                for a in addrs:
                    all_results.append(
                        {
                            "address": a,
                            "domain": dom,
                            "status": "dry_run",
                            "mx": mx_host,
                            "mx_pref": pref,
                        }
                    )
                continue

            last_err = None
            for pref, mx_host in mx_list:
                try:
                    per_addr = send_to_mx(
                        mx_host, addrs, envelope_from, delivery_mime, args
                    )
                except MXConnectionError as exc:
                    last_err = str(exc)
                    continue
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    continue

                for a, res in per_addr.items():
                    entry = {
                        "address": a,
                        "domain": dom,
                        "mx": mx_host,
                        "mx_pref": pref,
                    }
                    entry.update(res)
                    all_results.append(entry)
                break
            else:
                for a in addrs:
                    all_results.append(
                        {
                            "address": a,
                            "domain": dom,
                            "status": "failed",
                            "error": last_err or "all MX hosts failed",
                        }
                    )

        accepted = sum(1 for r in all_results if r.get("status") == "accepted")
        refused = sum(1 for r in all_results if r.get("status") == "refused")
        failed = sum(1 for r in all_results if r.get("status") == "failed")
        total = len(all_results)
        ok = (total > 0 and accepted == total) or (
            args.dry_run and total > 0 and failed == 0
        )
        out = {
            "ok": ok,
            "partial": 0 < accepted < total,
            "dry_run": args.dry_run,
            "email_id": args.email_id,
            "message_id": message_id,
            "bytes": size,
            "sha256": sha,
            "sha256_verified": sha_verified,
            "envelope_from": envelope_from,
            "helo": args.helo,
            "recipients": all_results,
            "summary": {
                "total": total,
                "accepted": accepted,
                "refused": refused,
                "failed": failed,
                "temporary": sum(
                    1 for r in all_results if r.get("temporary") is True
                ),
            },
        }
        print(json.dumps(out, ensure_ascii=False))

        if args.dry_run:
            return 0
        if accepted == 0:
            return 1
        if args.fail_on_error and accepted != total:
            return 1
        return 0
    finally:
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(
            json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)
