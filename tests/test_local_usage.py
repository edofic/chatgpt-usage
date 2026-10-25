import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import local_usage as local


PRIVATE = "conversation-and-tool-contents-must-never-be-stored"


def usage(n=1):
    return dict(input_tokens=100*n, cached_input_tokens=60*n, cache_write_input_tokens=5*n,
                output_tokens=20*n, reasoning_output_tokens=10*n, total_tokens=120*n)


def event(kind, payload, second=1):
    return dict(type=kind, timestamp=f"2026-10-25T00:00:{second:02d}Z", payload=payload)


def meta(identity="thread-1", **kwargs):
    return event("session_meta", dict(id=identity, timestamp="2026-10-25T00:00:00Z",
                 model_provider="openai", base_instructions=PRIVATE, **kwargs), 0)


def context(model="gpt-example", turn="turn-1"):
    return event("turn_context", dict(model=model, turn_id=turn, cwd=PRIVATE))


def snapshot(total, last=None, second=2):
    return event("event_msg", dict(type="token_count", info=dict(
        total_token_usage=total, last_token_usage=total if last is None else last)), second)


def response(identity="resp-1", second=2, turn="turn-1", **kwargs):
    return event("token_usage_record", dict(response_id=identity, turn_id=turn,
                 thread_id="thread-1", usage=usage(), thread_token_usage=usage(), **kwargs), second)


class LocalUsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sessions"
        self.root.mkdir()
        self.db_patch = patch.object(app, "DB_PATH", Path(self.temp.name) / "usage.sqlite")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.connection = app.database()
        self.addCleanup(self.connection.close)

    def write(self, items, filename="session.jsonl", append=False):
        path = self.root / filename
        with path.open("a" if append else "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
        return path

    def scan(self, **kwargs):
        return local.ingest(self.connection, self.root, **kwargs)

    def rows(self):
        return [dict(row) for row in self.connection.execute("SELECT * FROM local_events ORDER BY occurred_at")]

    def test_snapshot_deltas_duplicates_and_model_switch(self):
        self.write([meta(), context(), snapshot(usage()), snapshot(usage(), second=3),
                    context("another-model", "turn-2"), snapshot(usage(3), usage(2), 4)])
        run = self.scan()
        rows = self.rows()
        self.assertEqual(run["events_added"], 2)
        self.assertEqual([r["total_tokens"] for r in rows], [120, 240])
        self.assertEqual([r["model"] for r in rows], ["gpt-example", "another-model"])
        self.assertEqual(rows[1]["input_tokens"] + rows[1]["output_tokens"], 240)
        self.assertEqual(rows[1]["reasoning_output_tokens"], 20)
        self.assertEqual(self.scan()["events_added"], 0)

    def test_response_records_ignore_lagging_snapshot_mirrors(self):
        self.write([meta(), context(), response(), snapshot(usage(), second=3),
                    response("resp-2", 4), snapshot(usage(12), usage(), 5),
                    response("resp-2", 6), snapshot(usage(13), usage(), 7)])
        self.scan()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(sum(r["total_tokens"] for r in self.rows()), 240)
        self.assertTrue(all(r["method"] == "response" for r in self.rows()))

    def test_switch_from_legacy_to_response_records(self):
        self.write([meta(), context(), snapshot(usage()), response("resp-2", 3),
                    snapshot(usage(2), usage(), 4)])
        self.scan()
        self.assertEqual([r["method"] for r in self.rows()], ["snapshot", "response"])

    def test_first_cumulative_baseline_only_counts_last_response(self):
        self.write([meta(), context(), snapshot(usage(9), usage()), snapshot(usage(10), usage(), 3)])
        self.scan()
        self.assertEqual([r["total_tokens"] for r in self.rows()], [120, 120])
        self.assertEqual(local.summary(self.connection)["warnings"]["unattributed_baseline"], 1)

    def test_counter_reset_and_ambiguous_increments_are_conservative(self):
        self.write([meta(), context(), snapshot(usage(5)), snapshot(usage(), second=3),
                    snapshot(usage(4), usage(), 4), snapshot(usage(5), usage(), 5),
                    snapshot(usage(2), usage(), 6), snapshot(usage(3), usage(), 7)])
        self.scan()
        self.assertEqual([r["total_tokens"] for r in self.rows()], [600, 120, 120, 120])
        notices = local.summary(self.connection)["warnings"]
        self.assertEqual(notices["counter_reset"], 2)
        self.assertEqual(notices["ambiguous_increment"], 1)

    def test_copied_resume_and_fork_history_is_not_counted_again(self):
        original = [meta(), context(), snapshot(usage()), snapshot(usage(2), usage(), 3)]
        self.write(original, "parent.jsonl")
        self.scan()
        # Replay a copied log in a different file, then append new usage.
        self.write(original + [snapshot(usage(3), usage(), 4)], "resumed.jsonl")
        self.scan()
        self.assertEqual(len(self.rows()), 3)
        # Copied events can also exist in an independently named fork.
        fork_meta = meta("fork", forked_from_id="thread-1")
        fork_meta["payload"]["timestamp"] = "2026-10-25T00:00:05Z"
        self.write([fork_meta, meta(), context(), snapshot(usage(), second=2),
                    snapshot(usage(2), usage(), 6)], "fork.jsonl")
        self.scan()
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.rows()[-1]["session_key"], local.digest("fork"))

    def test_response_deduplication_across_copied_logs_and_restart(self):
        events = [meta(), context(), response()]
        self.write(events, "a.jsonl")
        self.scan()
        self.connection.close()
        self.connection = app.database()
        self.addCleanup(self.connection.close)
        self.write(events + [response("resp-new", 3)], "b.jsonl")
        self.scan()
        self.assertEqual(len(self.rows()), 2)

    def test_appends_use_saved_offset_without_reparsing(self):
        self.write([meta(), context(), response()])
        self.scan()
        with patch.object(local, "process_event", wraps=local.process_event) as parse:
            self.scan()
            self.assertEqual(parse.call_count, 0)
            self.write([response("resp-2", 3)], append=True)
            self.scan()
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(len(self.rows()), 2)

    def test_partial_final_line_is_retried_and_never_stored_raw(self):
        path = self.write([meta(), context()])
        line = json.dumps(response()).encode()
        with path.open("ab") as f:
            f.write(line[:50])
        self.assertEqual(self.scan()["status"], "backfilling")
        self.assertEqual(self.rows(), [])
        with path.open("ab") as f:
            f.write(line[50:] + b"\n")
        self.assertEqual(self.scan()["status"], "ok")
        self.assertEqual(len(self.rows()), 1)

    def test_truncation_replacement_and_rewrite_replay_idempotently(self):
        events = [meta(), context(), response()]
        self.write(events + [event("response_item", {"content": PRIVATE * 50})])
        self.scan()
        self.write(events)  # Truncate in place.
        self.scan()
        self.write(events + [response("resp-2", 3)], "replacement.jsonl")
        (self.root / "replacement.jsonl").replace(self.root / "session.jsonl")
        self.scan()
        self.assertEqual(len(self.rows()), 2)
        # Rewrite at the same size; an unchanged inode is not enough to skip it.
        self.write([meta(), context(), response("resp-3"), response("resp-4", 3)])
        self.scan()
        self.assertEqual(len(self.rows()), 4)

    def test_invalid_and_missing_usage_and_unknown_model(self):
        bad = usage(); bad["total_tokens"] += 50
        bad_reasoning = usage(); bad_reasoning["reasoning_output_tokens"] = 500
        for counters in (bad, bad_reasoning, {}, {**usage(), "input_tokens": True},
                         {**usage(), "input_tokens": -1}):
            self.assertIsNone(local.tokens(counters))
        path = self.write([meta(), snapshot(bad), event("event_msg", {"type": "token_count", "info": None}),
                           snapshot(usage(10), usage()), context(), response(turn="other-turn", second=3)])
        with path.open("a") as f:
            f.write("{bad json}\n")
        self.scan()
        self.assertEqual([r["model"] for r in self.rows()], ["unknown", "unknown"])
        self.assertIn("malformed_line", local.summary(self.connection)["warnings"])

    def test_missing_response_id_never_falls_back_to_mirror(self):
        broken = response(); broken["payload"].pop("response_id")
        self.write([meta(), context(), broken, snapshot(usage(), second=3)])
        self.scan()
        self.assertEqual(self.rows(), [])

    def test_auto_review_provider_and_utc_day_are_explicit(self):
        free = response()
        free["timestamp"] = "2026-10-25T00:30:00+02:00"
        self.write([meta(), context("codex-auto-review"), free,
                    context("other-model"), response("resp-2", 4)])
        self.scan()
        data = local.summary(self.connection)
        rows = self.rows()
        self.assertEqual([r["category"] for r in rows], ["auto_review", "model"])
        self.assertEqual(rows[0]["day"], "2026-10-24")
        self.assertEqual(rows[0]["provider"], "openai")
        self.assertEqual(data["scope"], "LOCAL MACHINE sessions only")
        self.assertEqual(data["timezone"], "UTC")
        self.assertEqual(len(data["daily"]), 2)

    def test_missing_unreadable_and_deleted_files_retain_history(self):
        path = self.write([meta(), context(), response()])
        self.scan()
        with patch.object(Path, "open", side_effect=PermissionError):
            run = self.scan()
        self.assertEqual(run["files_unreadable"], 1)
        self.assertEqual(run["status"], "partial")
        path.unlink()
        self.root.rmdir()
        self.assertEqual(self.scan()["status"], "root_missing")
        self.assertEqual(len(self.rows()), 1)

    def test_time_budget_and_throttle_resume_backfill(self):
        self.write([meta(), context(), response()])
        self.assertEqual(self.scan(budget_seconds=0)["pending_files"], 1)
        self.assertEqual(self.rows(), [])
        with patch.object(local, "ingest_file", wraps=local.ingest_file) as scan:
            self.scan(min_age=60)
            self.assertEqual(scan.call_count, 0)
        self.scan()
        self.assertEqual(len(self.rows()), 1)

    def test_no_private_text_or_raw_log_paths_are_persisted(self):
        self.write([meta(), context(), event("response_item", {"content": PRIVATE}), response()])
        self.scan()
        dump = "\n".join(self.connection.iterdump())
        self.assertNotIn(PRIVATE, dump)
        self.assertNotIn(str(self.root), dump)
        self.assertNotIn("resp-1", dump)
        self.assertNotIn("thread-1", dump)

    def test_missing_local_root_does_not_prevent_quota_collection(self):
        result = {"accountId": "test-account", "rateLimits": {"limitId": "codex",
                  "primary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 1_800_000_000}}}
        with patch.object(app, "SESSIONS_ROOT", self.root / "missing"), patch.object(
                app, "read_limits", return_value=result):
            self.assertEqual(app.collect(), 0)
        data = app.dashboard()
        self.assertEqual(data["windows"][0]["used_percent"], 25)
        self.assertEqual(data["local_usage"]["last_run"]["status"], "root_missing")
        # Dashboard reads cannot access session logs.
        with patch.object(local, "ingest", side_effect=AssertionError("must not scan")):
            self.assertEqual(app.dashboard()["windows"][0]["source_key"], "codex:primary")

    def test_local_only_does_not_invoke_account_rpc(self):
        self.write([meta(), context(), response()])
        with patch.object(app, "SESSIONS_ROOT", self.root), patch.object(
                app, "read_limits", side_effect=AssertionError("must not call RPC")):
            self.assertEqual(app.collect(local_only=True), 0)
        self.assertEqual(len(self.rows()), 1)

    def test_local_ingestion_survives_account_collection_failure(self):
        self.write([meta(), context(), response()])
        with patch.object(app, "SESSIONS_ROOT", self.root), patch.object(
                app, "read_limits", side_effect=app.CollectionError("codex_unavailable")):
            self.assertEqual(app.collect(), 1)
        data = app.dashboard()
        self.assertEqual(data["last_run"]["status"], "codex_unavailable")
        self.assertEqual(data["local_usage"]["coverage"]["events"], 1)
        self.assertEqual(data["windows"], [])


if __name__ == "__main__":
    unittest.main()
