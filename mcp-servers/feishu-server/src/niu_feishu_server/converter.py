"""cron -> RRULE converter.

Converts a 5-field cron expression into an RFC5545 RRULE string.
Returns None for unsupported patterns.
"""


# cron weekday (0=Sunday .. 6=Saturday) -> RRULE 2-letter code
_CRON_DOW_TO_RRULE = {
    0: "SU",
    1: "MO",
    2: "TU",
    3: "WE",
    4: "TH",
    5: "FR",
    6: "SA",
}


def _parse_dow_field(field: str) -> list[str] | None:
    """Parse the weekday field of a cron expression.

    Supports single values (``1``), comma-separated lists (``1,3,5``),
    and hyphenated ranges (``1-5``).  Returns *None* for any unsupported
    syntax (step expressions, out-of-range values, etc.).
    """
    days: list[str] = []
    for part in field.split(","):
        if "-" in part:
            # Could be a range like "1-5" or a step like "1-5/2"
            if "/" in part:
                return None  # step expressions not supported
            pieces = part.split("-")
            if len(pieces) != 2:
                return None
            try:
                lo, hi = int(pieces[0]), int(pieces[1])
            except ValueError:
                return None
            if not (0 <= lo <= 6 and 0 <= hi <= 6):
                return None
            for d in range(lo, hi + 1):
                days.append(_CRON_DOW_TO_RRULE[d])
        else:
            if "/" in part:
                return None  # step expressions not supported
            try:
                val = int(part)
            except ValueError:
                return None
            if not (0 <= val <= 6):
                return None
            days.append(_CRON_DOW_TO_RRULE[val])
    return days


def _parse_hour_field(field: str) -> list[int] | None:
    """Parse the hour field.  Supports comma-separated integers only."""
    hours: list[int] = []
    for part in field.split(","):
        if "/" in part:
            return None  # step expressions not supported
        if "-" in part:
            # hour ranges like 9-17 are not in the spec; reject
            return None
        try:
            val = int(part)
        except ValueError:
            return None
        if not (0 <= val <= 23):
            return None
        hours.append(val)
    return hours


def cron_to_rrule(cron_expr: str) -> str | None:
    """Convert a 5-field cron expression to an RFC5545 RRULE string.

    Supported patterns:
      - ``min hour * * *``         -> FREQ=DAILY
      - ``min hour * * dow-spec``  -> FREQ=WEEKLY;BYDAY=...

    Returns *None* for unsupported patterns (step expressions, non-wildcard
    day-of-month / month fields, wrong field count, etc.).
    """
    if not cron_expr or not cron_expr.strip():
        return None

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return None

    minute_s, hour_s, dom_s, month_s, dow_s = parts

    # Day-of-month and month must be wildcards
    if dom_s != "*":
        return None
    if month_s != "*":
        return None

    # Minute: must be a plain integer
    if "/" in minute_s or "-" in minute_s:
        return None
    try:
        minute = int(minute_s)
    except ValueError:
        return None
    if not (0 <= minute <= 59):
        return None

    # Hour
    hours = _parse_hour_field(hour_s)
    if hours is None:
        return None

    # Build common parts
    hour_str = ",".join(str(h) for h in sorted(hours))

    # Weekday field
    if dow_s == "*":
        # Daily frequency
        return f"FREQ=DAILY;BYHOUR={hour_str};BYMINUTE={minute}"
    else:
        days = _parse_dow_field(dow_s)
        if days is None:
            return None
        day_str = ",".join(days)
        return f"FREQ=WEEKLY;BYDAY={day_str};BYHOUR={hour_str};BYMINUTE={minute}"
