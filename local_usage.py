"""Incremental, metadata-only ingestion of local Codex rollout JSONL files.

Rollouts are an internal format, not an account-wide billing API. Prefer the
per-response records in newer logs; never add their token_count mirrors again.
Older logs only contribute independently verifiable snapshot increments.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,159}\Z")
MAX_LINE = 8 * 1024 * 1024


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS local_files (
            file_key TEXT PRIMARY KEY,
            device INTEGER NOT NULL, inode INTEGER NOT NULL,
            size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            offset INTEGER NOT NULL, anchor TEXT NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS local_events (
            event_key TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            day TEXT NOT NULL,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            category TEXT NOT NULL,
            method TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL,
            cache_write_input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            reasoning_output_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS local_events_day ON local_events(day);
        CREATE TABLE IF NOT EXISTS local_runs (
            id INTEGER PRIMARY KEY,
            checked_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            files_seen INTEGER NOT NULL,
            files_unreadable INTEGER NOT NULL,
            pending_files INTEGER NOT NULL,
            events_added INTEGER NOT NULL
        );
    """)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def name(value, fallback="unknown"):
    return value if isinstance(value, str) and NAME.fullmatch(value) else fallback


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or not 2000 <= dt.year <= 2100:
            return None
        return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError:
        return None


def tokens(value):
    """Cached input and reasoning output are subsets, never extra total tokens."""
    if not isinstance(value, dict):
        return None
    result = {}
    for field in TOKEN_FIELDS:
        number = value.get(field, 0 if field == "cache_write_input_tokens" else None)
        if type(number) is not int or not 0 <= number <= 10**15:
            return None
        result[field] = number
    if (result["total_tokens"] != result["input_tokens"] + result["output_tokens"]
            or result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"]
            or result["reasoning_output_tokens"] > result["output_tokens"]):
        return None
    return result


def warning(state, kind):
    counts = state.setdefault("warnings", {})
    counts[kind] = counts.get(kind, 0) + 1


def store_event(connection, state, key, at, usage, method, turn_id=None):
    if not at:
        warning(state, "invalid_timestamp")
        return
    # Forks can contain inherited events; their original response IDs / snapshot
    # fingerprints also deduplicate against the parent, regardless of scan order.
    if state.get("forked") and state.get("created_at") and at < state["created_at"]:
        return
    if not usage["total_tokens"]:
        return
    model = state.get("model", "unknown")
    if turn_id and digest(turn_id) != state.get("turn_id"):
        model = "unknown"
    category = "auto_review" if model.startswith("codex-auto-review") else "model"
    connection.execute("""INSERT OR IGNORE INTO local_events VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, state.get("session", state.get("fallback_session", digest("unknown"))), at, at[:10], model,
         state.get("provider", "unknown"), category, method,
         *(usage[field] for field in TOKEN_FIELDS)))


def process_event(connection, state, event):
    if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
        return
    payload = event["payload"]
    kind = event.get("type")
    at = timestamp(event.get("timestamp"))
    if kind == "session_meta":
        # The first meta is the file's identity. Forks may then embed parent meta.
        if "session" not in state:
            state["session"] = digest(name(payload.get("id")))
            state["created_at"] = timestamp(payload.get("timestamp"))
            state["forked"] = bool(payload.get("forked_from_id"))
            state["provider"] = name(payload.get("model_provider"))
        return
    if kind == "turn_context":
        state["model"] = name(payload.get("model"))
        state["turn_id"] = digest(name(payload.get("turn_id")))
        return
    if kind == "token_usage_record":
        # A format switch is sticky: token_count notifications can lag, repeat,
        # or use a different cumulative baseline from response records.
        state["records"] = True
        usage = tokens(payload.get("usage"))
        response_id = name(payload.get("response_id"), "")
        if not usage or not response_id:
            warning(state, "invalid_response_usage")
            return
        store_event(connection, state, digest("response:" + response_id), at, usage,
                    "response", name(payload.get("turn_id"), ""))
        return
    if kind != "event_msg" or payload.get("type") != "token_count" or state.get("records"):
        return
    info = payload.get("info")
    if not isinstance(info, dict):
        warning(state, "incomplete_counter")
        return
    total = tokens(info.get("total_token_usage"))
    last = tokens(info.get("last_token_usage"))
    if not total:
        # The next good snapshot must establish a fresh baseline.
        state.pop("previous", None)
        warning(state, "incomplete_counter")
        return
    previous = state.get("previous")
    state["previous"] = total
    if previous == total:
        return
    if previous is None:
        usage = last if last and all(last[k] <= total[k] for k in TOKEN_FIELDS) else None
        if usage != total:
            warning(state, "unattributed_baseline")
    elif any(total[k] < previous[k] for k in TOKEN_FIELDS):
        warning(state, "counter_reset")
        # Only a complete, single-response reset can be safely counted.
        usage = last if last == total else None
    else:
        delta = tokens({k: total[k] - previous[k] for k in TOKEN_FIELDS})
        usage = delta if delta and delta == last else None
        if usage is None:
            warning(state, "ambiguous_increment")
    if usage is None:
        return
    key = digest(json.dumps(["snapshot", at, total, last], sort_keys=True))
    store_event(connection, state, key, at, usage, "snapshot")


def anchor(stream, offset):
    stream.seek(max(0, offset - 256))
    tail = stream.read(min(offset, 256))
    stream.seek(0)
    head = stream.read(min(offset, 256))
    stream.seek(offset)
    return hashlib.sha256(head + tail).hexdigest()


def ingest_file(connection, path, file_key, deadline):
    saved = connection.execute("SELECT * FROM local_files WHERE file_key = ?", (file_key,)).fetchone()
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if (saved and saved["device"] == stat.st_dev and saved["inode"] == stat.st_ino
                and saved["size"] == stat.st_size and saved["mtime_ns"] == stat.st_mtime_ns
                and saved["offset"] == stat.st_size):
            return False
        offset = saved["offset"] if saved else 0
        if (not saved or saved["device"] != stat.st_dev or saved["inode"] != stat.st_ino
                or stat.st_size < saved["size"]
                or (stat.st_size == saved["size"] and stat.st_mtime_ns != saved["mtime_ns"])
                or anchor(stream, offset) != saved["anchor"]):
            offset, state = 0, {}
        else:
            state = json.loads(saved["state"])
        state["fallback_session"] = file_key
        stream.seek(offset)
        count = 0
        while stream.tell() < stat.st_size:
            if count % 128 == 0 and time.monotonic() >= deadline:
                break
            start = stream.tell()
            line = stream.readline(MAX_LINE + 1)
            if not line.endswith(b"\n"):
                if len(line) <= MAX_LINE:
                    stream.seek(start)  # Retry a partial final line on the next run.
                    break
                # Large conversation/tool records are irrelevant; discard bounded
                # chunks, retaining no content. Retry if the line is unfinished.
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE + 1)
                if not line:
                    stream.seek(start)
                    break
                warning(state, "oversized_line")
            else:
                try:
                    process_event(connection, state, json.loads(line))
                except (ValueError, UnicodeError):
                    warning(state, "malformed_line")
            count += 1
        offset = stream.tell()
        checkpoint = anchor(stream, offset)
    connection.execute("""INSERT OR REPLACE INTO local_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (file_key, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
                        offset, checkpoint, json.dumps(state, separators=(",", ":"))))
    return offset < stat.st_size


def ingest(connection, root, min_age=0, budget_seconds=8):
    """Caller holds collect.lock. GET requests never scan logs; each run is bounded."""
    now = int(time.time())
    run = connection.execute("SELECT * FROM local_runs ORDER BY id DESC LIMIT 1").fetchone()
    if min_age and run and now - run["checked_at"] < min_age:
        return dict(run)
    deadline = time.monotonic() + budget_seconds
    root = Path(root).expanduser()
    seen = unreadable = pending = added = 0
    status = "ok"
    try:
        # os.walk exposes directory permission failures (Path.rglob may hide them).
        if not root.is_dir():
            raise FileNotFoundError
        def walk_error(error):
            nonlocal unreadable
            unreadable += 1
        for directory, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
            dirs.sort()
            for filename in sorted(files):
                if not filename.endswith(".jsonl"):
                    continue
                seen += 1
                path = Path(directory) / filename
                file_key = digest(str(path.absolute()))
                if time.monotonic() >= deadline:
                    pending += 1
                    continue
                before = connection.total_changes
                try:
                    with connection:
                        if ingest_file(connection, path, file_key, deadline):
                            pending += 1
                    # local_files itself contributes one change only when scanned.
                    added += max(0, connection.total_changes - before - 1)
                except OSError:
                    unreadable += 1
        if unreadable:
            status = "partial"
        elif pending:
            status = "backfilling"
    except FileNotFoundError:
        status = "root_missing"
    except OSError:
        status = "root_unreadable"
    with connection:
        connection.execute("""INSERT INTO local_runs
            (checked_at, status, files_seen, files_unreadable, pending_files, events_added)
            VALUES (?, ?, ?, ?, ?, ?)""", (now, status, seen, unreadable, pending, added))
        connection.execute("DELETE FROM local_runs WHERE id < (SELECT MAX(id) - 100 FROM local_runs)")
    return dict(connection.execute("SELECT * FROM local_runs ORDER BY id DESC LIMIT 1").fetchone())


def summary(connection):
    run = connection.execute("""SELECT checked_at, status, files_seen,
        files_unreadable, pending_files, events_added FROM local_runs ORDER BY id DESC LIMIT 1""").fetchone()
    rows = connection.execute("""SELECT day, model, provider, category,
        COUNT(*) AS events, COUNT(DISTINCT session_key) AS sessions,
        SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
        SUM(cache_write_input_tokens) AS cache_write_input_tokens,
        SUM(output_tokens) AS output_tokens, SUM(reasoning_output_tokens) AS reasoning_output_tokens,
        SUM(total_tokens) AS total_tokens
        FROM local_events GROUP BY day, model, provider, category ORDER BY day DESC, total_tokens DESC""")
    coverage = dict(connection.execute("""SELECT MIN(day) AS first_day, MAX(day) AS last_day,
        COUNT(DISTINCT session_key) AS sessions, COUNT(*) AS events FROM local_events""").fetchone())
    warnings = {}
    for row in connection.execute("SELECT state FROM local_files"):
        for key, count in json.loads(row[0]).get("warnings", {}).items():
            warnings[key] = warnings.get(key, 0) + count
    return {"scope": "LOCAL MACHINE sessions only", "timezone": "UTC", "last_run": dict(run) if run else None,
            "coverage": coverage, "warnings": warnings, "daily": [dict(row) for row in rows]}
