import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


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


if __name__ == "__main__":
    unittest.main()
