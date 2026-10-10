"""
Opening hours as Google gives them (DaycareListing.hours):

    {"Monday": "7:30 AM–6 PM", "Friday": "7–11 AM, 2–6 PM", "Saturday": "Closed",
     "Sunday": "Open 24 hours"}

Ranges are turned into minutes after midnight, and shown in 24-hour time
("7:30–18:00"), as on the design.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SHORT = {d: d[:3] for d in DAYS}

# "7:30 AM–6 PM", "7–11 AM" (the start takes the end's AM/PM), "6 PM–2 AM".
# Google puts narrow no-break spaces before AM/PM; \s matches them.
RANGE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?\s*[–—-]\s*(\d{1,2})(?::(\d{2}))?\s*([AP]M)", re.I)


def to_minutes(hour, minute, meridiem):
    hour = int(hour) % 12 + (12 if meridiem.upper() == "PM" else 0)
    return hour * 60 + int(minute or 0)


def parse(text):
    """[(start, end), ...] in minutes after midnight; end may pass 24:00
    (1440) for hours that run past midnight. [] when closed or unreadable."""
    text = (text or "").strip()
    if "24 hours" in text.lower():
        return [(0, 24 * 60)]
    ranges = []
    for h1, m1, ap1, h2, m2, ap2 in RANGE.findall(text):
        end = to_minutes(h2, m2, ap2)
        start = to_minutes(h1, m1, ap1 or ap2)
        if not ap1 and start > end:     # "11–2 PM" is 11 AM to 2 PM
            start = to_minutes(h1, m1, "AM")
        if end <= start:                # past midnight
            end += 24 * 60
        ranges.append((start, end))
    return ranges


def fmt(minutes):
    minutes %= 24 * 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def label(text):
    """"7:30–18:00", "7:00–11:00, 14:00–18:00", "Closed", "Open 24 hours"."""
    ranges = parse(text)
    if ranges == [(0, 24 * 60)]:
        return "Open 24 hours"
    if ranges:
        return ", ".join(f"{fmt(a)}–{fmt(b)}" for a, b in ranges)
    return "Closed" if (text or "").strip().lower() == "closed" or not text else text


def now_in(time_zone):
    return datetime.now(ZoneInfo(time_zone or "UTC"))


def week(hours, now):
    """The seven days, Monday first: [{"day", "hours", "today"}]."""
    if not hours:
        return []
    today = DAYS[now.weekday()]
    return [{"day": d, "hours": label(hours.get(d, "")), "today": d == today} for d in DAYS if d in hours]


def summary(hours):
    """The most common opening hours and their days: "Mon–Fri · 7:30–18:00"."""
    if not hours:
        return ""
    groups = {}
    for d in DAYS:
        if parse(hours.get(d, "")):
            groups.setdefault(label(hours[d]), []).append(DAYS.index(d))
    if not groups:
        return ""
    hours_label, days = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(days) == 7:
        span = "Daily"
    elif days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        span = f"{SHORT[DAYS[days[0]]]}–{SHORT[DAYS[days[-1]]]}"
    else:
        span = ", ".join(SHORT[DAYS[i]] for i in days)
    return f"{span} · {hours_label}"


def status(hours, now):
    """{"open": True, "until": "18:00"} or {"open": False}; None without hours."""
    if not hours:
        return None
    minute = now.hour * 60 + now.minute
    today, yesterday = DAYS[now.weekday()], DAYS[now.weekday() - 1]
    for start, end in parse(hours.get(today, "")):
        if start <= minute < end:
            return {"open": True, "until": "" if end - start >= 24 * 60 else fmt(end)}
    for start, end in parse(hours.get(yesterday, "")):
        if minute + 24 * 60 < end:      # still open from last night
            return {"open": True, "until": fmt(end)}
    return {"open": False}
