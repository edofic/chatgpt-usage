import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ReadLimitsTest(unittest.TestCase):
    def run_server(self, body):
        # Use a real pipe to exercise message framing and initialization order.
        popen = subprocess.Popen
        script = "import json, select, sys, time\n" + body

        def start(*args, **kwargs):
            return popen([sys.executable, "-u", "-c", script], **kwargs)

        with patch.object(app.subprocess, "Popen", side_effect=start):
            return app.read_limits()

    def test_initialization_finishes_before_read_and_batched_reply_is_drained(self):
        result = self.run_server('''
request = json.loads(sys.stdin.buffer.readline())
assert request['method'] == 'initialize'
assert not select.select([sys.stdin.buffer], [], [], 0.05)[0]
print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.buffer.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.buffer.readline())
assert request['method'] == 'account/rateLimits/read'
sys.stdout.write(json.dumps({'method': 'account/updated', 'params': {}}) + '\\n'
                 + json.dumps({'id': request['id'], 'result': {'accountId': 'test'}}) + '\\n')
sys.stdout.flush()
time.sleep(5)
''')
        self.assertEqual(result, {"accountId": "test"})

    def test_initialization_error_is_reported_separately(self):
        with self.assertRaisesRegex(app.CollectionError, "^initialization_failed$"):
            self.run_server('''
request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({'id': request['id'], 'error': {'message': 'private details'}}), flush=True)
time.sleep(5)
''')

    def test_upstream_error_does_not_expose_raw_details(self):
        with self.assertRaisesRegex(app.CollectionError, "^rate_limits_unavailable$"):
            self.run_server('''
request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
sys.stdin.buffer.readline()
request = json.loads(sys.stdin.buffer.readline())
print(json.dumps({'id': request['id'], 'error': {'message': 'private details'}}), flush=True)
time.sleep(5)
''')


class UsageCyclesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = patch.object(app, "DB_PATH", Path(self.directory.name) / "usage.sqlite")
        self.database_path.start()
        self.addCleanup(self.database_path.stop)
        self.connection = app.database()
        self.addCleanup(self.connection.close)
        self.start = 1_800_000_000
        self.week = 10080 * 60

    def sample(self, at, reset, used, account="account", source="codex:primary", duration=10080):
        self.connection.execute("""INSERT INTO samples
            (sampled_at, account_id, source_key, limit_id, window_kind,
             duration_mins, resets_at, used_percent) VALUES (?, ?, ?, 'codex', ?, ?, ?, ?)""",
            (at, account, source, app.KINDS.get(duration, "other"), duration, reset, used))
        self.connection.commit()

    def cycles(self):
        return app.usage_cycles(self.connection, "account", "codex:primary")

    def test_overnight_idle_estimates_and_jitter_are_one_new_cycle(self):
        previous_reset = self.start + 4 * 86400
        self.sample(self.start - 600, previous_reset, 86)
        idle_resets = []
        for at in range(self.start, self.start + 8 * 3600 + 1, 600):
            idle_resets.append(at + self.week + 1)
            self.sample(at, idle_resets[-1], 0)
        anchored_reset = self.start + 8 * 3600 + self.week + 40
        self.sample(self.start + 8 * 3600 + 600, anchored_reset, 0)
        self.sample(self.start + 9 * 3600, anchored_reset + 1, 2)
        self.sample(self.start + 10 * 3600, anchored_reset, 17)

        cycles = self.cycles()
        self.assertEqual([c["resets_at"] for c in cycles], [previous_reset, anchored_reset])
        self.assertEqual(len(cycles[-1]["samples"]), 52)
        # A URL containing an earlier estimate still returns the entire window.
        self.assertEqual(app.series(self.connection, "account", "codex:primary", idle_resets[0]),
                         cycles[-1]["samples"])
        dashboard = app.dashboard()["windows"][0]
        self.assertEqual(dashboard["cycles"], [anchored_reset, previous_reset])
        self.assertEqual(dashboard["samples"], cycles[-1]["samples"])
        self.assertEqual(app.history("codex:primary", previous_reset), cycles[0]["samples"])
        # Read-time grouping preserves every original reset estimate.
        count = self.connection.execute("SELECT COUNT(DISTINCT resets_at) FROM samples").fetchone()[0]
        self.assertEqual(count, 52)

    def test_active_timestamp_variations_keep_all_samples(self):
        reset = self.start + self.week
        for offset, delta in ((0, 0), (600, 1), (1200, 0), (1800, -1)):
            self.sample(self.start + offset, reset + delta, 10)
        self.assertEqual(len(self.cycles()), 1)
        self.assertEqual(len(self.cycles()[0]["samples"]), 4)

    def test_real_reset_after_usage_starts_a_new_cycle(self):
        reset = self.start + self.week
        self.sample(self.start, reset, 80)
        self.sample(reset - 600, reset, 90)
        self.sample(reset + 600, reset + 600 + self.week, 0)
        self.sample(reset + 1200, reset + 600 + self.week, 1)
        self.assertEqual(len(self.cycles()), 2)
        self.assertEqual([len(c["samples"]) for c in self.cycles()], [2, 2])

    def test_fixed_unused_window_does_not_merge_a_different_reset(self):
        reset = self.start + self.week
        self.sample(self.start + 3600, reset, 0)
        self.sample(self.start + 7200, reset + 3600, 0)
        self.assertEqual(len(self.cycles()), 2)

    def test_gap_beyond_an_idle_deadline_starts_a_new_cycle(self):
        reset = self.start + self.week
        self.sample(self.start, reset, 0)
        self.sample(reset + 600, reset + 600 + self.week, 0)
        self.assertEqual(len(self.cycles()), 2)

    def test_pending_cycle_can_anchor_on_first_nonzero_sample(self):
        self.sample(self.start, self.start + self.week, 0)
        self.sample(self.start + 3600, self.start + 3600 + self.week, 0)
        self.sample(self.start + 7200, self.start + 7000 + self.week, 2)
        self.assertEqual(len(self.cycles()), 1)

    def test_accounts_sources_and_durations_stay_separate(self):
        reset = self.start + self.week
        self.sample(self.start, reset, 0)
        self.sample(self.start + 600, reset, 10, account="other-account")
        self.sample(self.start + 600, reset, 20, source="other:primary")
        self.sample(self.start + 1200, self.start + 18000, 0, duration=300)
        self.assertEqual([len(c["samples"]) for c in self.cycles()], [1, 1])
        self.assertEqual(app.series(self.connection, "account", "codex:primary", 123), [])

    def test_plan_change_moves_weekly_window_and_exposes_five_hour_window(self):
        self.sample(self.start, self.start + self.week, 1)
        result = {"accountId": "account", "rateLimitsByLimitId": {"codex": {
            "planType": "plus",
            "primary": {"windowDurationMins": 300, "usedPercent": 2,
                        "resetsAt": self.start + 18000},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 11,
                          "resetsAt": self.start + self.week},
        }}}
        with patch.object(app, "read_limits", return_value=result), \
                patch.object(app.time, "time", return_value=self.start + 600):
            self.assertEqual(app.collect(), 0)
        dashboard = app.dashboard()
        self.assertEqual(dashboard["last_run"]["sample_count"], 2)
        five, week = dashboard["windows"]
        self.assertEqual((five["window_kind"], five["source_key"], five["used_percent"]),
                         ("5h", "codex:primary", 2))
        self.assertEqual((week["window_kind"], week["source_key"], week["used_percent"]),
                         ("week", "codex:secondary", 11))
        self.assertEqual(len(five["samples"]), 1)
        self.assertEqual(five["samples"][0]["used_percent"], 2)

    def test_failed_collection_preserves_samples_and_next_success_recovers(self):
        self.sample(self.start, self.start + self.week, 1)
        with patch.object(app, "read_limits", side_effect=app.CollectionError("rate_limits_unavailable")):
            self.assertEqual(app.collect(), 1)
        failed = app.dashboard()
        self.assertFalse(failed["last_run"]["ok"])
        self.assertEqual(failed["windows"][0]["used_percent"], 1)
        result = {"accountId": "account", "rateLimits": {"limitId": "codex",
            "primary": {"windowDurationMins": 10080, "usedPercent": 2,
                        "resetsAt": self.start + self.week}}}
        with patch.object(app, "read_limits", return_value=result):
            self.assertEqual(app.collect(), 0)
        recovered = app.dashboard()
        self.assertTrue(recovered["last_run"]["ok"])
        self.assertEqual(recovered["windows"][0]["used_percent"], 2)


class AntigravityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "usage.sqlite"
        patcher = patch.object(app, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.start = 1_800_000_000
        self.week = 10080 * 60
        self.five = 300 * 60

    def sample_summary(self, *, three_p_reset=None, three_p_remaining=1.0, argon_remaining=1.0,
                       argon_reset="2026-11-28T16:30:34Z"):
        three_p_week = {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": three_p_remaining}
        three_p_5h = {"bucketId": "3p-5h", "window": "5h", "remainingFraction": three_p_remaining}
        if three_p_reset:
            three_p_week["resetTime"] = three_p_reset
            three_p_5h["resetTime"] = three_p_reset
        return {
            "accountId": "ag:user-123",
            "summary": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "description": "Models within this group: Gemini 4 Neon, Gemini 4 Hydrogen, Gemini 3.8 Flash",
                        "buckets": [
                            {"bucketId": "gemini-weekly", "window": "weekly",
                             "resetTime": "2026-11-28T11:23:22Z", "remainingFraction": 0.9273819},
                            {"bucketId": "gemini-5h", "window": "5h",
                             "resetTime": "2026-11-21T21:23:22Z", "remainingFraction": 0.96950996},
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "description": "Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B",
                        "buckets": [three_p_week, three_p_5h],
                    },
                    {
                        "displayName": "Gemini 4 Argon",
                        "description": "For paid Pro plans, Argon quota is separate.",
                        "buckets": [
                            {"bucketId": "gemini-argon-wtu-weekly", "window": "weekly",
                             "resetTime": argon_reset, "remainingFraction": argon_remaining},
                        ],
                    },
                ]
            },
        }

    def test_antigravity_parsing_handles_all_groups_and_omitted_resets(self):
        import antigravity
        rows = antigravity.parsed_samples(self.sample_summary(), self.start)
        by_key = {row[2]: row for row in rows}
        self.assertEqual(set(by_key), {
            "antigravity:gemini-weekly", "antigravity:gemini-5h",
            "antigravity:3p-weekly", "antigravity:3p-5h",
            "antigravity:gemini-argon-wtu-weekly",
        })
        self.assertEqual(by_key["antigravity:gemini-weekly"][3], "gemini")
        self.assertEqual(by_key["antigravity:gemini-weekly"][6], "week")
        self.assertAlmostEqual(by_key["antigravity:gemini-weekly"][9], 7.26181, places=4)
        self.assertEqual(by_key["antigravity:3p-5h"][3], "3p")
        self.assertEqual(by_key["antigravity:3p-5h"][8], 0)
        self.assertEqual(by_key["antigravity:gemini-argon-wtu-weekly"][3], "gemini-argon")
        self.assertEqual(by_key["antigravity:gemini-argon-wtu-weekly"][10], "antigravity")

        with self.assertRaisesRegex(antigravity.AntigravityError, "^antigravity_no_windows$"):
            antigravity.parsed_samples({"accountId": "ag:1", "summary": {"groups": []}}, self.start)

    def test_unanchored_idle_cycles_transition_to_active_and_back(self):
        connection = app.database()
        self.addCleanup(connection.close)

        def insert(at, reset, used, source="antigravity:3p-5h", limit_id="3p", duration=300):
            connection.execute("""INSERT INTO samples
                (sampled_at, account_id, source_key, limit_id, limit_name, window_kind,
                 duration_mins, resets_at, used_percent, provider)
                VALUES (?, 'ag:1', ?, ?, 'Claude and GPT models', ?, ?, ?, ?, 'antigravity')""",
                (at, source, limit_id, app.KINDS[duration], duration, reset, used))
            connection.commit()

        # Untouched with omitted resetTime -> idle cycle with resets_at=0.
        insert(self.start, 0, 0)
        insert(self.start + 600, 0, 0)
        dash_idle = app.dashboard()
        w = dash_idle["windows"][0]
        self.assertTrue(w["idle"])
        self.assertEqual(w["cycles"], [])
        self.assertEqual(w["samples"], [])

        # First request anchors a 5h window.
        anchored = self.start + 1200 + self.five
        insert(self.start + 1200, anchored, 12.5)
        insert(self.start + 1800, anchored, 25.0)
        dash_active = app.dashboard()
        w = dash_active["windows"][0]
        self.assertFalse(w["idle"])
        self.assertEqual(w["cycles"], [anchored])
        self.assertEqual(len(w["samples"]), 2)

        # Window expires and returns to omitted resetTime; past anchored cycle stays browseable.
        insert(anchored + 600, 0, 0)
        dash_after = app.dashboard()
        w = dash_after["windows"][0]
        self.assertTrue(w["idle"])
        self.assertEqual(w["cycles"], [anchored])
        self.assertEqual(w["samples"], [])
        self.assertEqual(len(app.history("antigravity:3p-5h", anchored, provider="antigravity")), 2)

    def test_floating_argon_reports_idle_until_used(self):
        connection = app.database()
        self.addCleanup(connection.close)
        for offset in (0, 600, 1200):
            at = self.start + offset
            connection.execute("""INSERT INTO samples
                (sampled_at, account_id, source_key, limit_id, limit_name, window_kind,
                 duration_mins, resets_at, used_percent, provider)
                VALUES (?, 'ag:1', 'antigravity:gemini-argon-wtu-weekly', 'gemini-argon',
                        'Gemini 4 Argon', 'week', 10080, ?, 0, 'antigravity')""",
                (at, at + self.week))
        connection.commit()
        dash = app.dashboard()
        self.assertTrue(dash["windows"][0]["idle"])

    def test_multiprovider_collection_isolates_failures(self):
        codex_result = {"accountId": "codex-acct", "rateLimitsByLimitId": {"codex": {
            "primary": {"windowDurationMins": 300, "usedPercent": 14, "resetsAt": self.start + self.five},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 26, "resetsAt": self.start + self.week},
        }}}
        with patch.object(app, "ANTIGRAVITY_TOKEN_PATH", "/tmp/fake-token.json"), \
                patch.object(app, "read_limits", side_effect=app.CollectionError("rate_limits_unavailable")), \
                patch.object(app, "read_antigravity_limits", return_value=self.sample_summary()), \
                patch.object(app.time, "time", return_value=self.start):
            self.assertEqual(app.collect(), 1)

        dash = app.dashboard()
        self.assertFalse(dash["providers"]["codex"]["last_run"]["ok"])
        self.assertTrue(dash["providers"]["antigravity"]["last_run"]["ok"])
        self.assertEqual(dash["providers"]["antigravity"]["last_run"]["sample_count"], 5)
        self.assertEqual(len(dash["windows"]), 5)

        # Now Codex recovers while Antigravity fails; both providers' latest samples remain visible.
        import antigravity
        with patch.object(app, "ANTIGRAVITY_TOKEN_PATH", "/tmp/fake-token.json"), \
                patch.object(app, "read_limits", return_value=codex_result), \
                patch.object(app, "read_antigravity_limits",
                             side_effect=antigravity.AntigravityError("antigravity_quota_unavailable")), \
                patch.object(app.time, "time", return_value=self.start + 600):
            self.assertEqual(app.collect(), 1)

        dash2 = app.dashboard()
        self.assertTrue(dash2["providers"]["codex"]["last_run"]["ok"])
        self.assertFalse(dash2["providers"]["antigravity"]["last_run"]["ok"])
        providers = {w["provider"] for w in dash2["windows"]}
        self.assertEqual(providers, {"codex", "antigravity"})
        self.assertEqual(len(dash2["windows"]), 7)

    def test_legacy_database_migrates_cleanly(self):
        import sqlite3
        raw = sqlite3.connect(self.db_path)
        raw.executescript("""
            CREATE TABLE samples (
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
            CREATE TABLE collection_runs (
                id INTEGER PRIMARY KEY,
                checked_at INTEGER NOT NULL,
                ok INTEGER NOT NULL,
                status TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO samples (sampled_at, account_id, source_key, limit_id, window_kind,
                                 duration_mins, resets_at, used_percent)
            VALUES (1800000000, 'legacy-acct', 'codex:secondary', 'codex', 'week', 10080, 1800604800, 15);
            INSERT INTO collection_runs (checked_at, ok, status, sample_count)
            VALUES (1800000000, 1, 'ok', 1);
        """)
        raw.close()

        dash = app.dashboard()
        self.assertEqual(len(dash["windows"]), 1)
        self.assertEqual(dash["windows"][0]["provider"], "codex")
        self.assertEqual(dash["windows"][0]["used_percent"], 15)
        self.assertTrue(dash["providers"]["codex"]["last_run"]["ok"])


if __name__ == "__main__":
    unittest.main()
