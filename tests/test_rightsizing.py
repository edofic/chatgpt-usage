import http.client
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import rightsizing as rs


BASE = 1_800_000_000


def event(offset, amount):
    return {"at": BASE + offset, "since": BASE + max(0, offset - 600), "amount": amount}


def history(events, days=14):
    return {"scope": "test", "source": "codex:primary", "events": events,
            "timestamps": list(range(BASE, BASE + days * rs.DAY + 1, 600)),
            "reset_count": 3, "corrections": 0}


class ReplayTest(unittest.TestCase):
    def test_both_limits_debit_the_same_work_and_denied_work_is_not_queued(self):
        # One Plus week = 20 points, one block = 3. The five-hour budget
        # admits 3 of the first 5 points; a fresh block then admits 2 more.
        result = rs.replay([event(600, 5), event(600 + rs.FIVE_HOURS, 2)], 20, 3, 0)
        self.assertEqual(result["served_points"], 5)
        self.assertEqual(result["blocked_points"], 2)
        self.assertEqual(result["max_wait_seconds"], rs.FIVE_HOURS)

    def test_weekly_limit_remains_after_five_hour_reset(self):
        events = [event(i * rs.FIVE_HOURS + 600, 3) for i in range(8)]
        result = rs.replay(events, 20, 3, 0)
        self.assertEqual(result["served_points"], 20)
        self.assertEqual(result["blocked_points"], 4)

    def test_week_and_block_reset_at_expiry_and_only_on_next_activity(self):
        events = [event(600, 20), event(600 + rs.WEEK, 20)]
        result = rs.replay(events, 20, None, 0)
        self.assertEqual(result["served_points"], 40)
        self.assertEqual(result["blocked_points"], 0)
        pressure = rs.five_hour_pressure([event(600, 3), event(600 + rs.FIVE_HOURS, 3)], 3)
        self.assertEqual(pressure["windows"], 2)
        self.assertEqual(pressure["over_limit"], 0)

    def test_reserve_reduces_weekly_budget_without_shrinking_the_five_hour_cap(self):
        result = rs.replay([event(600, 10)], 10, None, 20)
        self.assertEqual(result["served_points"], 8)
        self.assertEqual(result["blocked_points"], 2)
        result = rs.replay([event(600, 3)], 20, 3, 20)
        self.assertEqual(result["served_points"], 3)

    def test_weekly_fit_and_session_fit_can_disagree(self):
        result = rs.compare(history([event(600, 5)]), {"reserve_percent": 0}, BASE + 14 * rs.DAY)
        plus = result["plans"][0]
        self.assertTrue(plus["weekly_fit"])
        self.assertEqual(plus["status"], "interruptions")
        self.assertEqual(plus["sessions"]["over_limit"], 1)
        self.assertEqual(result["suggested_plan"], "pro0")

    def test_exclusion_ranges_are_half_open_idempotent_and_do_not_shorten_history(self):
        raw = history([event(600, 20), event(1200, 1), event(1800, 2)])
        exclusion = {"start": BASE + 600, "end": BASE + 1800, "label": "Experiment"}
        result = rs.compare(raw, {"exclusions": [exclusion, exclusion]}, BASE + 14 * rs.DAY)
        self.assertEqual(result["history"]["total_points"], 2)
        self.assertEqual(result["history"]["excluded_points"], 21)
        self.assertEqual(result["history"]["days"], 14)
        self.assertEqual(sum(d["excluded"] for d in result["days"]), 21)
        self.assertEqual(len(raw["events"]), 3)

    def test_latest_rolling_week_detects_work_across_old_reset_boundaries(self):
        raw = history([event(8 * rs.DAY, 11), event(10 * rs.DAY, 11)])
        result = rs.compare(raw, {"reserve_percent": 0}, BASE + 14 * rs.DAY)
        self.assertAlmostEqual(result["plans"][0]["peak_week_percent"], 110)
        self.assertEqual(result["plans"][0]["status"], "too_small")

    def test_weekly_peak_between_daily_boundaries_is_not_missed(self):
        raw = history([event(rs.DAY + 12 * 3600, 11), event(8 * rs.DAY + 6 * 3600, 11)])
        result = rs.compare(raw, {"reserve_percent": 0}, BASE + 14 * rs.DAY)
        self.assertAlmostEqual(result["plans"][0]["peak_week_percent"], 110)
        self.assertEqual(result["plans"][0]["status"], "too_small")

    def test_partial_history_and_empty_retained_history_never_recommend_a_downgrade(self):
        for raw in (history([event(600, 1)], days=6), history([])):
            result = rs.compare(raw, {}, BASE + 14 * rs.DAY)
            self.assertIsNone(result["suggested_plan"])
            self.assertTrue(all(p["status"] == "insufficient" for p in result["plans"]))
        self.assertIsNone(rs.compare(None, {}, BASE)["history"])

    def test_day_exclusions_use_real_dst_midnights(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Europe/Ljubljana")
        at = int(datetime(2026, 10, 25, 12, tzinfo=zone).timestamp())
        raw = history([])
        raw["timestamps"] = [at - rs.DAY, at + rs.DAY]
        result = rs.compare(raw, {"timezone": "Europe/Ljubljana"}, at + rs.DAY)
        day = next(d for d in result["days"] if d["date"] == "2026-10-25")
        self.assertEqual(day["end"] - day["start"], 25 * 3600)

    def test_invalid_settings_are_rejected_including_non_finite_values(self):
        invalid = [None, [], {"current_multiple": 0}, {"current_multiple": math.nan},
                   {"current_multiple": True}, {"period_days": True}, {"period_days": 500},
                   {"pro_multiples": [5]}, {"timezone": "../../etc/passwd"},
                   {"exclusions": [{"start": 100, "end": 99}]},
                   {"exclusions": [{"start": 1, "end": 2, "label": "x" * 161}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                rs.settings(value)


class EvidenceAndApiTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(app, "DB_PATH", Path(directory.name) / "usage.sqlite")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = app.database()
        self.addCleanup(self.connection.close)

    def sample(self, offset, used, reset, source="codex:primary", account="a", duration=10080):
        self.connection.execute("""INSERT INTO samples (sampled_at,account_id,source_key,
            limit_id,window_kind,duration_mins,resets_at,used_percent)
            VALUES (?,?,?,'codex',?,?,?,?)""", (BASE + offset, account, source,
            app.KINDS[duration], duration, BASE + reset, used))
        self.connection.commit()

    def test_first_balance_jitter_corrections_and_early_resets(self):
        self.sample(0, 50, rs.WEEK)
        self.sample(600, 55, rs.WEEK + 1)
        self.sample(1200, 53, rs.WEEK)
        self.sample(1800, 55, rs.WEEK)
        self.sample(2400, 56, rs.WEEK)
        self.sample(3000, 0, rs.WEEK + 3000)
        self.sample(3600, 4, rs.WEEK + 3000)
        evidence = rs.evidence(self.connection, app.usage_cycles)
        self.assertEqual([e["amount"] for e in evidence["events"]], [5, 1, 4])
        self.assertEqual(evidence["corrections"], 1)
        self.assertEqual(evidence["reset_count"], 1)

    def test_one_bucket_and_latest_account_avoid_double_counting(self):
        self.sample(0, 0, rs.WEEK)
        self.sample(600, 5, rs.WEEK)
        self.sample(0, 0, rs.WEEK, source="codex:secondary")
        self.sample(600, 5, rs.WEEK, source="codex:secondary")
        self.sample(1200, 0, rs.WEEK, account="b")
        self.sample(1800, 2, rs.WEEK, account="b")
        evidence = rs.evidence(self.connection, app.usage_cycles)
        self.assertEqual(sum(e["amount"] for e in evidence["events"]), 2)
        # Five-hour-only accounts have no comparable weekly history.
        self.sample(2400, 1, rs.FIVE_HOURS, account="c", duration=300)
        self.assertIsNone(rs.evidence(self.connection, app.usage_cycles))

    def test_antigravity_samples_do_not_affect_codex_rightsizing(self):
        self.sample(0, 0, rs.WEEK)
        self.sample(600, 7, rs.WEEK)
        self.connection.execute("""INSERT INTO samples (sampled_at,account_id,source_key,
            limit_id,window_kind,duration_mins,resets_at,used_percent,provider)
            VALUES (?,?,?,'gemini','week',10080,?,50,'antigravity')""",
            (BASE + 1200, "ag:user", "antigravity:gemini-weekly", BASE + rs.WEEK))
        self.connection.commit()
        evidence = rs.evidence(self.connection, app.usage_cycles)
        self.assertEqual(evidence["source"], "codex:primary")
        self.assertEqual(sum(e["amount"] for e in evidence["events"]), 7)

    def test_http_comparison_is_read_only_and_serves_assets(self):
        self.sample(0, 0, rs.WEEK)
        self.sample(600, 5, rs.WEEK)
        def request(method, path, body=None):
            # Exercise the real HTTP parser and handler without opening a socket.
            body = (body or "").encode()
            wire = f"{method} {path} HTTP/1.0\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            class Connection:
                def __init__(self, data):
                    self.data, self.output = data, io.BytesIO()
                def makefile(self, *args):
                    return io.BytesIO(self.data)
                def sendall(self, data):
                    self.output.write(data)
            connection = Connection(wire)
            app.Handler(connection, ("127.0.0.1", 1), None)
            response = http.client.HTTPResponse(Connection(connection.output.getvalue()))
            response.begin()
            return response.status, response.getheader("Content-Type"), response.read()
        status, _, payload = request("GET", "/api/rightsizing")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["history"]["total_points"], 5)
        status, _, payload = request("POST", "/api/rightsizing", json.dumps({"current_multiple": 10}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["plans"][0]["weekly_capacity"], 10)
        for body in ('[]', '{broken', '{"five_hour_percent":0}'):
            self.assertEqual(request("POST", "/api/rightsizing", body)[0], 400)
        for path, content in (("/rightsizing.js", "text/javascript"), ("/rightsizing.css", "text/css")):
            status, content_type, _ = request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(content_type.startswith(content))
        self.assertEqual(request("GET", "/../../rightsizing.py")[0], 404)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
