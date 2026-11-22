"""Read-only Google Antigravity quota collector using existing OAuth credentials."""

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
USER_AGENT = "antigravity/cli/99.0.0 linux/amd64"
WINDOWS = {"5h": ("5h", 300), "weekly": ("week", 10080)}
HTTP_TIMEOUT = 10
AGY_BIN = os.environ.get("AGY_BIN", "agy")
AGY_REFRESH_TIMEOUT = 25
EXPIRY_MARGIN_SECS = 60.0

_cache_lock = threading.Lock()
_token_cache = {}


class AntigravityError(Exception):
    pass


def clear_cache():
    with _cache_lock:
        _token_cache.clear()


def _load_credentials(token_path):
    path = Path(token_path).expanduser()
    try:
        resolved = path.resolve()
        stat = resolved.stat()
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise AntigravityError("antigravity_unconfigured") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AntigravityError("antigravity_auth_invalid") from exc
    if not isinstance(data, dict):
        raise AntigravityError("antigravity_auth_invalid")
    cache_key = (str(resolved), stat.st_mtime_ns)
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    refresh_token = data.get("refresh_token")
    token_uri = data.get("token_uri") or DEFAULT_TOKEN_URI
    if all(isinstance(v, str) and v for v in (client_id, client_secret, refresh_token, token_uri)):
        return {
            "kind": "oauth_client",
            "path": resolved,
            "cache_key": cache_key,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "token_uri": token_uri,
        }
    token = data.get("token")
    if isinstance(token, dict):
        access_token = token.get("access_token")
        if isinstance(access_token, str) and access_token:
            return {
                "kind": "agy_token",
                "path": resolved,
                "cache_key": cache_key,
                "access_token": access_token,
                "expiry": parse_reset_time(token.get("expiry")),
                "has_refresh_token": isinstance(token.get("refresh_token"), str) and bool(token.get("refresh_token")),
            }
    raise AntigravityError("antigravity_auth_invalid")


def _http_json(url, *, method="GET", headers=None, body=None, timeout=HTTP_TIMEOUT, error_code="antigravity_quota_unavailable"):
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        raise AntigravityError(f"{error_code}:{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AntigravityError(error_code) from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AntigravityError(error_code) from exc
    if not isinstance(parsed, dict):
        raise AntigravityError(error_code)
    return parsed


def _refresh_access_token(creds):
    form = urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    try:
        token_reply = _http_json(
            creds["token_uri"],
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=form,
            error_code="antigravity_token_refresh_failed",
        )
    except AntigravityError as exc:
        raise AntigravityError("antigravity_token_refresh_failed") from exc
    access_token = token_reply.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AntigravityError("antigravity_token_refresh_failed")
    expires_in = token_reply.get("expires_in", 3600)
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not math.isfinite(expires_in):
        expires_in = 3600
    expires_at = time.monotonic() + max(60.0, float(expires_in) - 60.0)
    return access_token, expires_at


def _fetch_account_id(access_token):
    try:
        info = _http_json(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            error_code="antigravity_account_unavailable",
        )
    except AntigravityError:
        return "ag:default"
    sub = info.get("sub")
    if isinstance(sub, str) and sub:
        return f"ag:{sub}"
    return "ag:default"


def _refresh_via_agy(token_path):
    """Trigger native agy OAuth refresh via a read-only ACP session/list call."""
    app_dir = Path(token_path).expanduser().resolve().parent
    if not app_dir.name or app_dir == app_dir.parent:
        raise AntigravityError("antigravity_token_refresh_failed")
    env = dict(os.environ, HOME=str(app_dir.parent))
    try:
        process = subprocess.Popen(
            [AGY_BIN, "acp", "--gemini-dir", ".", "--app-data-dir", app_dir.name, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=env,
        )
    except OSError as exc:
        raise AntigravityError("antigravity_token_refresh_failed") from exc
    try:
        deadline = time.monotonic() + AGY_REFRESH_TIMEOUT
        buffer = b""

        def send(request):
            try:
                process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                process.stdin.flush()
            except OSError as exc:
                raise AntigravityError("antigravity_token_refresh_failed") from exc

        def receive(request_id):
            nonlocal buffer
            while time.monotonic() < deadline:
                if b"\n" not in buffer:
                    remaining = max(0.0, deadline - time.monotonic())
                    ready, _, _ = select.select([process.stdout], [], [], min(1.0, remaining))
                    if not ready:
                        if process.poll() is not None:
                            raise AntigravityError("antigravity_token_refresh_failed")
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise AntigravityError("antigravity_token_refresh_failed")
                    buffer += chunk
                    if len(buffer) > 1024 * 1024:
                        raise AntigravityError("antigravity_token_refresh_failed")
                    continue
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise AntigravityError("antigravity_token_refresh_failed") from exc
                if not isinstance(message, dict):
                    raise AntigravityError("antigravity_token_refresh_failed")
                if message.get("id") == request_id:
                    return message
            raise AntigravityError("antigravity_token_refresh_failed")

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        if "error" in receive(1):
            raise AntigravityError("antigravity_token_refresh_failed")
        send({"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {}})
        if "error" in receive(2):
            raise AntigravityError("antigravity_token_refresh_failed")
    except OSError as exc:
        raise AntigravityError("antigravity_token_refresh_failed") from exc
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def _get_session(creds, force_refresh=False):
    key = creds["cache_key"]
    now = time.monotonic()
    with _cache_lock:
        cached = _token_cache.get(key)
        if not force_refresh and cached and cached["expires_at"] > now:
            return cached["access_token"], cached["account_id"], False

    refreshed = False
    if creds["kind"] == "oauth_client":
        access_token, expires_at = _refresh_access_token(creds)
        refreshed = True
    else:
        current = creds
        wall_now = time.time()
        expired = current["expiry"] > 0 and current["expiry"] <= wall_now + EXPIRY_MARGIN_SECS
        if force_refresh or expired:
            previous_token = current["access_token"]
            _refresh_via_agy(current["path"])
            refreshed = True
            reloaded = _load_credentials(current["path"])
            if reloaded["kind"] != "agy_token":
                raise AntigravityError("antigravity_token_refresh_failed")
            reloaded_expired = reloaded["expiry"] > 0 and reloaded["expiry"] <= time.time() + EXPIRY_MARGIN_SECS
            if reloaded_expired or (force_refresh and reloaded["access_token"] == previous_token):
                raise AntigravityError("antigravity_token_refresh_failed")
            current = reloaded
            key = current["cache_key"]
            wall_now = time.time()
        access_token = current["access_token"]
        if current["expiry"] > 0:
            remaining = max(0.0, current["expiry"] - wall_now - EXPIRY_MARGIN_SECS)
            expires_at = time.monotonic() + remaining
        else:
            expires_at = time.monotonic() + EXPIRY_MARGIN_SECS

    with _cache_lock:
        previous = _token_cache.get(key)
        if not previous:
            path_str = key[0]
            for (cached_path, _), entry in _token_cache.items():
                if cached_path == path_str and entry.get("account_id") != "ag:default":
                    previous = entry
                    break
        account_id = previous["account_id"] if previous and previous.get("account_id") != "ag:default" else None
    if not account_id:
        account_id = _fetch_account_id(access_token)
    with _cache_lock:
        _token_cache[key] = {
            "access_token": access_token,
            "expires_at": expires_at,
            "account_id": account_id,
        }
    return access_token, account_id, refreshed


def read_limits(token_path):
    """Fetch live Antigravity quota summary without logging credentials."""
    creds = _load_credentials(token_path)
    for attempt in range(2):
        access_token, account_id, refreshed = _get_session(creds, force_refresh=(attempt > 0))
        try:
            summary = _http_json(
                QUOTA_URL,
                method="POST",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                body=b"{}",
                error_code="antigravity_quota_unavailable",
            )
            return {"accountId": account_id, "summary": summary}
        except AntigravityError as exc:
            if attempt == 0 and not refreshed and str(exc) == "antigravity_quota_unavailable:401":
                continue
            raise AntigravityError("antigravity_quota_unavailable") from exc
    raise AntigravityError("antigravity_quota_unavailable")


def bucket_limit_id(bucket_id):
    if bucket_id.startswith("gemini-argon"):
        return "gemini-argon"
    if bucket_id.startswith("gemini-"):
        return "gemini"
    if bucket_id.startswith("3p-"):
        return "3p"
    for suffix in ("-weekly", "-5h"):
        if bucket_id.endswith(suffix) and len(bucket_id) > len(suffix):
            return bucket_id[:-len(suffix)]
    return bucket_id


def parse_reset_time(value):
    if not isinstance(value, str) or not value.strip():
        return 0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.10 fromisoformat rejects non-3/6 fractional digit counts (e.g. Go RFC3339Nano).
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        return 0
    ts = int(round(dt.timestamp()))
    return ts if ts > 0 else 0


def parsed_samples(result, sampled_at):
    if not isinstance(result, dict):
        raise AntigravityError("antigravity_quota_unavailable")
    account_id = result.get("accountId")
    if not isinstance(account_id, str) or not account_id:
        raise AntigravityError("antigravity_missing_account")
    summary = result.get("summary")
    if not isinstance(summary, dict):
        raise AntigravityError("antigravity_quota_unavailable")
    groups = summary.get("groups")
    if not isinstance(groups, list):
        raise AntigravityError("antigravity_no_windows")

    rows = []
    seen_sources = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = group.get("displayName")
        if not isinstance(group_name, str) or not group_name.strip():
            group_name = None
        else:
            group_name = group_name.strip()
        group_desc = group.get("description")
        if not isinstance(group_desc, str) or not group_desc.strip():
            group_desc = None
        else:
            group_desc = group_desc.strip()
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_id = bucket.get("bucketId")
            if not isinstance(bucket_id, str) or not bucket_id.strip():
                continue
            bucket_id = bucket_id.strip()
            window_spec = WINDOWS.get(bucket.get("window"))
            if window_spec is None:
                continue
            window_kind, duration_mins = window_spec
            remaining = bucket.get("remainingFraction")
            if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
                    or not math.isfinite(remaining) or not 0.0 <= remaining <= 1.0):
                continue
            source_key = f"antigravity:{bucket_id}"
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            limit_id = bucket_limit_id(bucket_id)
            limit_name = group_name or (bucket.get("displayName") if isinstance(bucket.get("displayName"), str) else None)
            resets_at = parse_reset_time(bucket.get("resetTime"))
            used_percent = round(max(0.0, min(100.0, (1.0 - float(remaining)) * 100.0)), 6)
            rows.append((
                sampled_at,
                account_id,
                source_key,
                limit_id,
                limit_name,
                group_desc,
                window_kind,
                duration_mins,
                resets_at,
                used_percent,
                "antigravity",
            ))
    if not rows:
        raise AntigravityError("antigravity_no_windows")
    return rows
