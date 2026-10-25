#!/usr/bin/env python3
"""Serve synthetic usage history in a temporary database, without running Codex."""

import math
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def seed(now):
    week = 7 * 86400
    start = now - (4 * 86400 + 6 * 3600)
    reset = start + week
    current = []
    for sampled_at in range(start, now + 1, 3600):
        hours = (sampled_at - start) / 3600
        used = (hours * 0.12 + (9 if hours >= 24 else 0)
                + (8 if hours >= 59 else 0) + (6.76 if hours >= 88 else 0))
        current.append((sampled_at, "demo-account", "codex:secondary", "codex",
                        None, "week", 10080, reset, round(used, 2)))
    previous = []
    for hours in range(0, 168, 2):
        sampled_at = start - week + hours * 3600
        used = min(97, hours * 0.48 + max(0, math.sin(hours / 12)) * 4)
        previous.append((sampled_at, "demo-account", "codex:secondary", "codex",
                         None, "week", 10080, start, round(used, 2)))
    connection = app.database()
    try:
        with connection:
            connection.executemany("""INSERT INTO samples
                (sampled_at, account_id, source_key, limit_id, limit_name,
                 window_kind, duration_mins, resets_at, used_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", previous + current)
            connection.execute("""INSERT INTO collection_runs
                (checked_at, ok, status, sample_count) VALUES (?, 1, 'ok', 1)""", (now,))
    finally:
        connection.close()
    return current[-1]


def main():
    with tempfile.TemporaryDirectory(prefix="chatgpt-usage-demo-") as directory:
        app.DB_PATH = Path(directory) / "usage.sqlite"
        latest = seed(int(time.time()))

        def collect_demo(min_age=0):
            now = int(time.time())
            connection = app.database()
            try:
                with connection:
                    connection.execute("""INSERT OR REPLACE INTO samples
                        (sampled_at, account_id, source_key, limit_id, limit_name,
                         window_kind, duration_mins, resets_at, used_percent)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (now, *latest[1:]))
                    connection.execute("""INSERT INTO collection_runs
                        (checked_at, ok, status, sample_count) VALUES (?, 1, 'ok', 1)""", (now,))
            finally:
                connection.close()
            return 0

        # Browser refreshes stay synthetic; no Codex executable or login is used.
        app.collect = collect_demo
        print("Demo: synthetic data in a temporary database; Ctrl-C to stop.", flush=True)
        try:
            app.serve()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
