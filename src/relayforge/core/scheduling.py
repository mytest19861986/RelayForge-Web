"""Workspace-local calendar calculation with explicit UTC outputs."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


class SystemClock:
    def now(self):
        return datetime.now(UTC)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("SCHEDULE_REQUIRES_TIMEZONE")
    return value.astimezone(UTC)


def scheduled_for(policy, timezone, *, clock=None):
    now = _utc((clock or SystemClock()).now())
    mode = policy.get("mode", "IMMEDIATE")
    if mode == "IMMEDIATE":
        return now
    if mode == "AT_TIMESTAMP":
        return _utc(policy["at_timestamp"])
    if mode == "INTERVAL_MINUTES":
        minutes = policy["interval_minutes"]
        if type(minutes) is not int or not 1 <= minutes <= 525600:
            raise ValueError("INVALID_INTERVAL")
        anchor = _utc(policy["anchor"]) if "anchor" in policy else now
        spacing = timedelta(minutes=minutes)
        return anchor if anchor > now else anchor + (1 + (now - anchor) // spacing) * spacing
    if mode not in {"DAILY_AT", "WEEKLY_AT"}:
        raise ValueError("INVALID_SCHEDULE_MODE")
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    wall = time.fromisoformat(policy["time"])
    if wall.tzinfo is not None:
        raise ValueError("EXPECTED_LOCAL_WALL_TIME")
    weekday = policy.get("weekday") if mode == "WEEKLY_AT" else None
    if mode == "WEEKLY_AT" and (type(weekday) is not int or not 0 <= weekday <= 6):
        raise ValueError("INVALID_WEEKDAY")
    for delta in range(8):
        day = local.date() + timedelta(days=delta)
        if weekday is not None and day.weekday() != weekday:
            continue
        # fold=0 chooses one send for repeated times. Gaps roll forward by the offset change.
        candidate = datetime.combine(day, wall, zone).astimezone(UTC)
        if candidate >= now:
            return candidate
    raise ValueError("INVALID_CALENDAR_SCHEDULE")


def catch_up(scheduled, now, policy="HOLD_FOR_REVIEW", *, spacing_sec=60, position=0):
    scheduled, now = _utc(scheduled), _utc(now)
    if scheduled >= now:
        return "SCHEDULE", scheduled
    if policy == "HOLD_FOR_REVIEW":
        return "NEEDS_REVIEW", None
    if policy == "SKIP_EXPIRED":
        return "SKIP_EXPIRED", None
    if policy == "GRADUAL_THROTTLED" and 1 <= spacing_sec <= 86400 and position >= 0:
        return "SCHEDULE", now + timedelta(seconds=position * spacing_sec)
    raise ValueError("INVALID_CATCH_UP_POLICY")
