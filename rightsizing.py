"""Counterfactual plan comparisons from account-wide allowance snapshots.

All amounts are percentage points of the recorded plan. No token-to-quota or
price conversion is implied. The simulation never writes to usage history.
"""

import bisect
import hashlib
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DAY = 86400
WEEK = 7 * DAY
FIVE_HOURS = 18000
DEFAULTS = {"current_multiple": 5, "five_hour_percent": 15,
            "reserve_percent": 10, "pro_multiples": [5, 10, 25],
            "period_days": 30, "timezone": "UTC", "exclusions": []}


def settings(value):
    if not isinstance(value, dict):
        raise ValueError("Comparison settings must be an object.")
    result = dict(DEFAULTS)

    def number(raw, name, low, high):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name} must be a number.")
        if not math.isfinite(raw) or not low <= raw <= high:
            raise ValueError(f"{name} must be between {low} and {high}.")
        return float(raw)

    for key, low, high in (("current_multiple", .1, 1000),
                           ("five_hour_percent", 1, 100),
                           ("reserve_percent", 0, 90)):
        result[key] = number(value.get(key, result[key]), key, low, high)
    multiples = value.get("pro_multiples", DEFAULTS["pro_multiples"])
    if not isinstance(multiples, list) or len(multiples) != 3:
        raise ValueError("Provide three Pro allowance multiples.")
    result["pro_multiples"] = [number(x, "Pro multiple", .1, 1000) for x in multiples]
    period = value.get("period_days", 30)
    if isinstance(period, bool) or period not in (0, 7, 30, 60, 90):
        raise ValueError("Choose all history, 7, 30, 60 or 90 days.")
    result["period_days"] = period
    timezone = value.get("timezone", "UTC")
    if not isinstance(timezone, str) or len(timezone) > 100:
        raise ValueError("Invalid time zone.")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Unknown time zone.") from None
    result["timezone"] = timezone
    exclusions = value.get("exclusions", [])
    if not isinstance(exclusions, list) or len(exclusions) > 200:
        raise ValueError("Use at most 200 exclusions.")
    result["exclusions"] = []
    for item in exclusions:
        if not isinstance(item, dict):
            raise ValueError("Invalid exclusion.")
        start = number(item.get("start"), "Exclusion start", 0, 4102444800)
        end = number(item.get("end"), "Exclusion end", 0, 4102444800)
        label = item.get("label", "Exceptional usage")
        if end <= start or not isinstance(label, str) or len(label) > 160:
            raise ValueError("Exclusions need an end after the start and a short label.")
        result["exclusions"].append({"start": start, "end": end, "label": label})
    return result


def evidence(connection, cycles_for):
    """Choose one latest weekly bucket, avoiding duplicate primary/secondary data."""
    account = connection.execute(
        "SELECT account_id FROM samples WHERE provider = 'codex' ORDER BY sampled_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if not account:
        return None
    account = account["account_id"]
    source = connection.execute("""SELECT source_key FROM samples
        WHERE provider = 'codex' AND account_id = ? AND duration_mins = 10080
        ORDER BY sampled_at DESC, CASE limit_id WHEN 'codex' THEN 0 ELSE 1 END, id DESC LIMIT 1""",
        (account,)).fetchone()
    if not source:
        return None
    cycles = [c for c in cycles_for(connection, account, source["source_key"])
              if c["duration_mins"] == 10080]
    events, timestamps = [], []
    corrections = 0
    for index, cycle in enumerate(cycles):
        samples = cycle["samples"]
        # The first reading is an existing balance, not newly observed work.
        high = samples[0]["used_percent"] if index == 0 else 0
        previous_used = high
        previous = timestamps[-1] if timestamps else samples[0]["sampled_at"]
        for sample in samples:
            at, used = sample["sampled_at"], sample["used_percent"]
            if used < previous_used:
                corrections += 1
            amount = max(0, used - high)
            if amount:
                events.append({"at": at, "since": previous, "amount": amount})
            high = max(high, used)
            previous_used = used
            previous = at
            timestamps.append(at)
    if not timestamps:
        return None
    return {"scope": hashlib.sha256((account + ':' + source["source_key"]).encode()).hexdigest()[:20],
            "source": source["source_key"], "events": events, "timestamps": timestamps,
            "reset_count": max(0, len(cycles) - 1), "corrections": corrections}


def replay(events, weekly_capacity, five_capacity, reserve_percent):
    """Keep the original request times; work denied by a cap is not queued.

    Weekly and five-hour windows start on the first observed activity after
    their previous window expires. Historical promotional resets are not replayed.
    Both limits debit the same admitted work, rather than double-counting it.
    """
    budget = weekly_capacity * (1 - reserve_percent / 100)
    week_end = five_end = 0
    weekly_used = five_used = served = blocked = 0.0
    affected = set()
    waits = []
    for event in events:
        at, amount = event["at"], event["amount"]
        if at >= week_end:
            week_end, weekly_used = at + WEEK, 0.0
        if five_capacity is not None and at >= five_end:
            five_end, five_used = at + FIVE_HOURS, 0.0
        weekly_left = max(0, budget - weekly_used)
        five_left = max(0, five_capacity - five_used) if five_capacity is not None else math.inf
        admitted = min(amount, weekly_left, five_left)
        served += admitted
        weekly_used += admitted
        five_used += admitted
        denied = amount - admitted
        if denied > 1e-8:
            blocked += denied
            affected.add(week_end)
            waits.append(max(week_end - at if weekly_left < amount else 0,
                             five_end - at if five_left < amount else 0))
    return {"served_points": served, "blocked_points": blocked,
            "covered_percent": served / (served + blocked) * 100 if served + blocked else None,
            "affected_weeks": len(affected), "max_wait_seconds": max(waits, default=0)}


def five_hour_pressure(events, capacity):
    """An independent session-pressure diagnostic, even when weekly quota runs out."""
    if capacity is None:
        return {"windows": 0, "over_limit": 0, "examples": [], "peak_multiple": None}
    windows = []
    for event in events:
        at = event["at"]
        if not windows or at >= windows[-1]["end"]:
            windows.append({"start": at, "end": at + FIVE_HOURS,
                            "amount": 0, "first_blocked_at": None})
        window = windows[-1]
        window["amount"] += event["amount"]
        if window["amount"] > capacity + 1e-8 and window["first_blocked_at"] is None:
            window["first_blocked_at"] = at
    exceeded = [w for w in windows if w["first_blocked_at"] is not None]
    for window in exceeded:
        window["multiple"] = window["amount"] / capacity
        window["wait_seconds"] = window["end"] - window["first_blocked_at"]
    return {"windows": len(windows), "over_limit": len(exceeded),
            "peak_multiple": max((w["amount"] / capacity for w in windows), default=0),
            "examples": sorted(exceeded, key=lambda w: w["amount"], reverse=True)[:5]}


def compare(history, raw_settings, now):
    config = settings(raw_settings)
    if history is None:
        return {"settings": config, "history": None, "plans": [], "days": []}
    timestamps = history["timestamps"]
    end = timestamps[-1]
    start = max(timestamps[0], end - config["period_days"] * DAY) if config["period_days"] else timestamps[0]
    # Only increments whose previous sample is inside the period are attributable.
    observed = [e for e in history["events"] if e["at"] > start and e["since"] >= start]
    exclusions = config["exclusions"]
    retained = [e for e in observed if not any(x["start"] <= e["at"] < x["end"] for x in exclusions)]
    total = sum(e["amount"] for e in retained)
    original = sum(e["amount"] for e in observed)
    span = end - start
    sample_times = [t for t in timestamps if start <= t <= end]
    gaps = [b - a for a, b in zip(sample_times, sample_times[1:]) if b - a > 1800]
    zone = ZoneInfo(config["timezone"])
    daily = {}
    date = datetime.fromtimestamp(start, zone).date()
    last_date = datetime.fromtimestamp(end, zone).date()
    while date <= last_date:
        day_start = int(datetime.combine(date, datetime.min.time(), zone).timestamp())
        day_end = int(datetime.combine(date + timedelta(days=1), datetime.min.time(), zone).timestamp())
        daily[date.isoformat()] = {"date": date.isoformat(), "start": day_start,
                                  "end": day_end, "amount": 0, "excluded": 0}
        date += timedelta(days=1)
    for event in observed:
        day = daily[datetime.fromtimestamp(event["at"], zone).date().isoformat()]
        day["amount"] += event["amount"]
    for event in retained:
        day = daily[datetime.fromtimestamp(event["at"], zone).date().isoformat()]
        day["excluded"] += event["amount"]
    for day in daily.values():
        # Store both original consumption and the amount removed, not synthetic zeros.
        day["excluded"] = day["amount"] - day["excluded"]
    event_times = [e["at"] for e in retained]
    cumulative = [0]
    for event in retained:
        cumulative.append(cumulative[-1] + event["amount"])
    weeks = []
    cursor = start
    while cursor + WEEK <= end:
        left = bisect.bisect_right(event_times, cursor)
        right = bisect.bisect_right(event_times, cursor + WEEK)
        weeks.append({"start": cursor, "end": cursor + WEEK,
                      "amount": cumulative[right] - cumulative[left]})
        cursor += DAY
    # Include the latest complete 168-hour span even when not aligned with the first.
    if span >= WEEK and (not weeks or weeks[-1]["end"] != end):
        left = bisect.bisect_right(event_times, end - WEEK)
        weeks.append({"start": end - WEEK, "end": end, "amount": total - cumulative[left]})
    # A burst spanning two calendar-day boundaries can be missed by daily checks.
    # A rolling maximum occurs at an increase, or at an observation boundary.
    # Check every such end time, retaining only the peak as an extra chart bar.
    if weeks:
        busiest = max(weeks, key=lambda w: w["amount"])
        for index, at in enumerate(event_times):
            if at - WEEK < start:
                continue
            left = bisect.bisect_right(event_times, at - WEEK)
            amount = cumulative[index + 1] - cumulative[left]
            if amount > busiest["amount"]:
                busiest = {"start": at - WEEK, "end": at, "amount": amount}
        if not any(w["end"] == busiest["end"] for w in weeks):
            weeks.append(busiest)
            weeks.sort(key=lambda w: w["end"])
    plans = []
    profiles = [("plus", "Plus", 1, config["five_hour_percent"])]
    profiles += [(f"pro{i}", f"Pro · {multiple:g}×", multiple, None)
                 for i, multiple in enumerate(config["pro_multiples"])]
    peak = max((w["amount"] for w in weeks), default=None)
    for key, name, multiple, fraction in profiles:
        capacity = 100 * multiple / config["current_multiple"]
        five_capacity = capacity * fraction / 100 if fraction is not None else None
        budget = capacity * (1 - config["reserve_percent"] / 100)
        weekly_fit = peak <= budget + 1e-8 if peak is not None else None
        pressure = five_hour_pressure(retained, five_capacity)
        status = ("insufficient" if not weeks or not total else
                  "too_small" if not weekly_fit else
                  "interruptions" if pressure["over_limit"] else "fits")
        plans.append({"id": key, "name": name, "multiple": multiple,
                      "weekly_capacity": capacity, "weekly_budget": budget,
                      "five_hour_capacity": five_capacity, "status": status,
                      "weekly_fit": weekly_fit, "peak_week_percent": peak / capacity * 100 if peak is not None else None,
                      "average_week_percent": total / span * WEEK / capacity * 100 if span > 0 else None,
                      "replay": replay(retained, capacity, five_capacity, config["reserve_percent"]),
                      "sessions": pressure})
    fits = sorted((p for p in plans if p["status"] == "fits"), key=lambda p: p["multiple"])
    return {"settings": config, "history": {"scope": history["scope"], "source": history["source"],
            "start": start, "end": end, "days": span / DAY, "samples": len(sample_times),
            "total_points": total, "original_points": original, "excluded_points": original - total,
            "resets": history["reset_count"], "corrections": history["corrections"],
            "gap_count": len(gaps), "largest_gap_seconds": max(gaps, default=0),
            "stale": now - end > 3600, "complete_week_spans": len(weeks)},
            "plans": plans, "days": list(daily.values()), "weeks": weeks,
            "suggested_plan": fits[0]["id"] if fits else None}
