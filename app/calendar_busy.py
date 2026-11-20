"""Your calendar, read-only: when you're busy, so suggested dates avoid it, and
calendar links for the slot you pick.

Reads the calendar's *secret iCal address* (Google Calendar: Settings -> your
calendar -> "Secret address in iCal format"; iCloud: share as a public calendar).
That's one paste and read-only by construction: no OAuth app, nothing can write.
Adding the event happens when you tap a link: Google Calendar's own "add event"
page, or an .ics file for Apple Calendar.

Only busy *times* go to the model: event titles, places and attendees never leave this module.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 5_000_000
CACHE_S = 600
_cache: dict = {}


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))


def _fetch(url: str, http: httpx.Client | None = None) -> bytes:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url.removeprefix("webcal://")
    if not url.startswith("https://"):
        raise ValueError("The calendar address must start with https:// or webcal://")
    client = http or httpx.Client(timeout=20, follow_redirects=True)
    try:
        r = client.get(url)
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            raise ValueError("That calendar is too large to read.")
        return r.content
    finally:
        if http is None:
            client.close()


def busy(url: str, days: int = 14, http: httpx.Client | None = None, now: datetime | None = None) -> list[tuple]:
    """(start, end) local datetimes you're busy in the next `days` days, sorted. All-day
    events count as busy all day. Raises on a bad address; callers treat that as 'no calendar'."""
    import icalendar
    import recurring_ical_events

    key = (url, days)
    if http is None and key in _cache and time.monotonic() - _cache[key][0] < CACHE_S:
        return _cache[key][1]
    zone = tz()
    start = (now or datetime.now(zone)).astimezone(zone)
    end = start + timedelta(days=days)
    cal = icalendar.Calendar.from_ical(_fetch(url, http))
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT" or str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue  # "free" events and cancelled ones don't block time
        s, e = ev.decoded("DTSTART"), ev.decoded("DTEND") if ev.get("DTEND") else None
        if isinstance(s, date) and not isinstance(s, datetime):
            s = datetime.combine(s, datetime.min.time(), zone)
            e = datetime.combine(e, datetime.min.time(), zone) if isinstance(e, date) else s + timedelta(days=1)
        else:
            s = s if s.tzinfo else s.replace(tzinfo=zone)
            e = (e if e.tzinfo else e.replace(tzinfo=zone)) if isinstance(e, datetime) else s + timedelta(hours=1)
        out.append((s.astimezone(zone), e.astimezone(zone)))
    out.sort()
    if http is None:
        _cache[key] = (time.monotonic(), out)
    return out


def context_block(blocks: list[tuple], days: int = 14, now: datetime | None = None) -> str:
    """Busy times by day, for the prompt. Days with nothing listed are free."""
    zone = tz()
    today = (now or datetime.now(zone)).astimezone(zone).date()
    by_day: dict = {}
    for s, e in blocks:
        d = s.date()
        while d <= e.date() and d < today + timedelta(days=days):
            if d >= today:
                day_start = datetime.combine(d, datetime.min.time(), zone)
                a, b = max(s, day_start), min(e, day_start + timedelta(days=1))
                if b > a:
                    whole = a == day_start and b == day_start + timedelta(days=1)
                    by_day.setdefault(d, []).append("all day" if whole else f"{a:%H:%M}-{b:%H:%M}")
            d += timedelta(days=1)
    lines = [f"Your calendar, next {days} days (times you're BUSY, {zone.key}; any time not listed is free):"]
    for i in range(days):
        d = today + timedelta(days=i)
        lines.append(f"- {d:%a %b %-d}: {', '.join(by_day[d]) if d in by_day else 'free'}")
    return "\n".join(lines)


def overlaps(blocks: list[tuple], start: datetime, end: datetime) -> bool:
    return any(s < end and start < e for s, e in blocks)


def parse_local(value: str) -> datetime | None:
    """'2026-11-28T19:00' (local time, from the model) as an aware datetime, or None."""
    try:
        d = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=tz())


def google_url(title: str, start: datetime, end: datetime, location: str = "", details: str = "") -> str:
    """Google Calendar's prefilled 'add event' page; nothing is saved until you tap Save."""
    fmt = "%Y%m%dT%H%M%S"
    zone = tz()
    q = {"action": "TEMPLATE", "text": title[:200],
         "dates": f"{start.astimezone(zone):{fmt}}/{end.astimezone(zone):{fmt}}", "ctz": zone.key,
         "location": location[:300], "details": details[:1500]}
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


def ics(title: str, start: datetime, end: datetime, location: str = "", details: str = "", uid: str = "") -> bytes:
    import icalendar

    cal = icalendar.Calendar()
    cal.add("prodid", "-//Sidequest//EN")
    cal.add("version", "2.0")
    ev = icalendar.Event()
    ev.add("uid", uid or f"{start:%Y%m%dT%H%M}-{abs(hash(title))}@sidequest")
    ev.add("summary", title[:200])
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("dtstamp", datetime.now(tz()))
    if location:
        ev.add("location", location[:300])
    if details:
        ev.add("description", details[:1500])
    cal.add_component(ev)
    return cal.to_ical()


def ics_many(events: list[dict]) -> bytes:
    """Several events in one .ics (e.g. every flight on a trip). Each: summary, start, end, location, details, uid."""
    import icalendar

    cal = icalendar.Calendar()
    cal.add("prodid", "-//Sidequest//EN")
    cal.add("version", "2.0")
    for e in events:
        ev = icalendar.Event()
        ev.add("uid", e["uid"])
        ev.add("summary", e["summary"][:200])
        ev.add("dtstart", e["start"])
        ev.add("dtend", e["end"])
        ev.add("dtstamp", datetime.now(tz()))
        if e.get("location"):
            ev.add("location", e["location"][:300])
        if e.get("details"):
            ev.add("description", e["details"][:1500])
        cal.add_component(ev)
    return cal.to_ical()


def parse_in(value: str, zone: str | None) -> datetime | None:
    """A local time at a given IANA zone (a flight's departure airport), or in APP_TIMEZONE."""
    try:
        d = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if d.tzinfo:
        return d
    try:
        return d.replace(tzinfo=ZoneInfo(zone)) if zone else d.replace(tzinfo=tz())
    except Exception:  # noqa: BLE001 - an unknown zone name from the model
        return d.replace(tzinfo=tz())
