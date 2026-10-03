#!/usr/bin/env python3
"""Collect Codex plan limits and serve a small read-only dashboard."""

import fcntl
import json
import math
import os
import select
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "usage.sqlite"))
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
KINDS = {300: "5h", 10080: "week"}
RESET_TOLERANCE = 60  # Reported reset timestamps can vary by a few seconds.


class CollectionError(Exception):
    pass


def database():
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY,
            sampled_at INTEGER NOT NULL,
            account_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            limit_id TEXT NOT NULL,
            limit_name TEXT,
            window_kind TEXT NOT NULL,
            duration_mins INTEGER NOT NULL,
            resets_at INTEGER NOT NULL,
            used_percent REAL NOT NULL,
            UNIQUE (sampled_at, account_id, source_key)
        );
        CREATE INDEX IF NOT EXISTS samples_account_source_cycle
          ON samples (account_id, source_key, resets_at, sampled_at);
        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY,
            checked_at INTEGER NOT NULL,
            ok INTEGER NOT NULL,
            status TEXT NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0
        );
    """)
    return connection


def read_limits():
    """Use the documented app-server RPC and never persist or print its raw reply."""
    try:
        process = subprocess.Popen(
            [CODEX_BIN, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise CollectionError("codex_unavailable") from exc
    try:
        requests = (
            {"method": "initialize", "id": 1, "params": {"clientInfo": {
                "name": "chatgpt_usage", "title": "ChatGPT Usage", "version": "0.1.0"}}},
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 2},
        )
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], min(1, deadline - time.monotonic()))
            if not ready:
                if process.poll() is not None:
                    raise CollectionError("codex_exited")
                continue
            line = process.stdout.readline()
            if not line:
                raise CollectionError("codex_exited")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CollectionError("invalid_response") from exc
            if message.get("id") == 2:
                if "error" in message:
                    raise CollectionError("rate_limits_unavailable")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CollectionError("invalid_response")
                return result
        raise CollectionError("timeout")
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def parsed_samples(result, sampled_at):
    account_id = result.get("accountId")
    if not isinstance(account_id, str) or not account_id:
        raise CollectionError("missing_account")
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = result.get("rateLimits")
        buckets = {single.get("limitId", "codex"): single} if isinstance(single, dict) else {}
    rows = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket_id, str) or not isinstance(bucket, dict):
            continue
        limit_id = bucket.get("limitId") or bucket_id
        if not isinstance(limit_id, str):
            continue
        limit_name = bucket.get("limitName")
        if not isinstance(limit_name, str):
            limit_name = None
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            duration = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            if (isinstance(used, bool) or not isinstance(used, (int, float))
                    or not math.isfinite(used) or not 0 <= used <= 100
                    or isinstance(duration, bool) or not isinstance(duration, int)
                    or duration <= 0 or isinstance(reset, bool)
                    or not isinstance(reset, int) or reset <= 0):
                continue
            rows.append((sampled_at, account_id, f"{limit_id}:{slot}", limit_id,
                         limit_name, KINDS.get(duration, "other"), duration, reset, float(used)))
    if not rows:
        raise CollectionError("no_windows")
    return rows


def collect(min_age=0):
    """Serialize cron and browser refreshes, and reuse a recent result."""
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DB_PATH.parent / "collect.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return collect_locked(min_age)


def collect_locked(min_age):
    now = int(time.time())
    connection = database()
    try:
        if min_age:
            latest = connection.execute("""SELECT checked_at FROM collection_runs
                ORDER BY id DESC LIMIT 1""").fetchone()
            if latest and now - latest["checked_at"] < min_age:
                return 0
        try:
            rows = parsed_samples(read_limits(), now)
        except CollectionError as exc:
            connection.execute("INSERT INTO collection_runs (checked_at, ok, status) VALUES (?, 0, ?)",
                               (now, str(exc)))
            connection.commit()
            print(f"usage collection failed: {exc}", file=sys.stderr)
            return 1
        with connection:
            connection.executemany("""INSERT OR REPLACE INTO samples
                (sampled_at, account_id, source_key, limit_id, limit_name,
                 window_kind, duration_mins, resets_at, used_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            connection.execute("""INSERT INTO collection_runs
                (checked_at, ok, status, sample_count) VALUES (?, 1, 'ok', ?)""",
                               (now, len(rows)))
        print(f"stored {len(rows)} usage window(s)")
        return 0
    finally:
        connection.close()


def row_dict(row):
    return dict(row) if row is not None else None


def usage_cycles(connection, account_id, source_key):
    """Group reset estimates into observed cycles without altering raw samples."""
    cycles = []
    awaiting_activity = False
    rows = connection.execute("""SELECT sampled_at, used_percent, resets_at, duration_mins
        FROM samples WHERE account_id = ? AND source_key = ?
        ORDER BY sampled_at, id""", (account_id, source_key))
    for row in rows:
        start = row["resets_at"] - row["duration_mins"] * 60
        # An unused allowance can report a full window from the check time until
        # activity anchors its reset. Those moving estimates are one idle cycle.
        floating = (row["used_percent"] == 0
                    and abs(start - row["sampled_at"]) <= RESET_TOLERANCE)
        same_cycle = False
        if cycles and cycles[-1]["duration_mins"] == row["duration_mins"]:
            cycle = cycles[-1]
            same_cycle = abs(row["resets_at"] - cycle["resets_at"]) <= RESET_TOLERANCE
            if not same_cycle and awaiting_activity:
                same_cycle = (row["sampled_at"] < cycle["resets_at"]
                              and cycle["samples"][0]["sampled_at"] - RESET_TOLERANCE
                              <= start <= row["sampled_at"] + RESET_TOLERANCE)
        if not same_cycle:
            cycles.append({"resets_at": row["resets_at"],
                           "duration_mins": row["duration_mins"],
                           "reset_estimates": set(), "samples": []})
            awaiting_activity = floating
        else:
            awaiting_activity = awaiting_activity and floating
        cycle = cycles[-1]
        cycle["resets_at"] = row["resets_at"]
        cycle["reset_estimates"].add(row["resets_at"])
        cycle["samples"].append({"sampled_at": row["sampled_at"],
                                 "used_percent": row["used_percent"]})
    return cycles


def series(connection, account_id, source_key, reset):
    for cycle in reversed(usage_cycles(connection, account_id, source_key)):
        if reset in cycle["reset_estimates"]:
            return cycle["samples"]
    return []


def dashboard():
    connection = database()
    try:
        run = row_dict(connection.execute("""SELECT checked_at, ok, status, sample_count
            FROM collection_runs ORDER BY id DESC LIMIT 1""").fetchone())
        account = connection.execute("""SELECT account_id FROM samples
            ORDER BY sampled_at DESC, id DESC LIMIT 1""").fetchone()
        windows = []
        if account:
            rows = connection.execute("""SELECT s.* FROM samples s JOIN (
                SELECT source_key, MAX(id) AS latest_id FROM samples
                WHERE account_id = ? GROUP BY source_key
            ) latest ON latest.latest_id = s.id
            WHERE s.window_kind IN ('5h', 'week')
            ORDER BY CASE s.limit_id WHEN 'codex' THEN 0 ELSE 1 END,
                     s.limit_id, CASE s.window_kind WHEN '5h' THEN 0 ELSE 1 END""",
                (account["account_id"],)).fetchall()
            for row in rows:
                item = dict(row)
                item.pop("account_id")
                item.pop("id")
                cycles = usage_cycles(connection, account["account_id"], row["source_key"])
                cycles = [cycle for cycle in cycles
                          if cycle["duration_mins"] == row["duration_mins"]]
                item["cycles"] = [cycle["resets_at"] for cycle in reversed(cycles[-16:])]
                item["samples"] = cycles[-1]["samples"]
                windows.append(item)
        return {"last_run": run, "server_time": int(time.time()), "windows": windows}
    finally:
        connection.close()


def history(source_key, reset):
    connection = database()
    try:
        account = connection.execute("""SELECT account_id FROM samples
            ORDER BY sampled_at DESC, id DESC LIMIT 1""").fetchone()
        if not account:
            return []
        return series(connection, account["account_id"], source_key, reset)
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        payload = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/health":
            self.send_json({"ok": True})
        elif path.path == "/api/dashboard":
            self.send_json(dashboard())
        elif path.path == "/api/series":
            query = parse_qs(path.query)
            source = query.get("source", [""])[0]
            reset = query.get("reset", [""])[0]
            if len(source) > 200 or not source or not reset.isdecimal():
                self.send_json({"error": "invalid request"}, 400)
            else:
                self.send_json({"samples": history(source, int(reset))})
        elif path.path in ("/", "/index.html", "/icon.svg"):
            filename = "icon.svg" if path.path == "/icon.svg" else "index.html"
            payload = (ROOT / "static" / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if filename.endswith(".svg") else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self):
        request = urlparse(self.path)
        if request.path != "/api/refresh":
            self.send_error(404)
            return
        force = parse_qs(request.query).get("force") == ["1"]
        collect(min_age=0 if force else 60)
        self.send_json(dashboard())


def serve():
    connection = database()
    connection.close()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("serve", "collect"):
        print("usage: app.py {serve|collect}", file=sys.stderr)
        sys.exit(2)
    sys.exit(collect() if sys.argv[1] == "collect" else serve())
