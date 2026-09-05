"""
scheduler.py -- STAGE 8. Picking a time and creating the event.

Two important design choices, both worth defending out loud:

1. The LLM never picks a time. `pick_slot` is a pure function: same inputs,
   same output, unit-testable, no network. The model is only told what was
   booked.

2. The event is created BEFORE the email is written. The real time and the
   real event id are then injected into the drafting prompt as facts. This
   makes it structurally impossible to promise a call that does not exist.
   If the send later fails, `cancel` runs as compensation.

Double-booking is prevented in two places: FreeBusy from Google, and a
UNIQUE constraint on thread_key in our own events table.
"""
import os, re, uuid, random
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple
from .models import CalendarEvent

DAY_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


# ====================================================== timezone resolution
TZ_HINTS = [
    (r"\b(IST|India|Mumbai|Delhi|Bengaluru|Bangalore|Chennai|Kolkata|Pune|Hyderabad)\b", "Asia/Kolkata"),
    (r"\b(GMT|BST|London|UK)\b", "Europe/London"),
    (r"\b(EST|EDT|New York|NYC)\b", "America/New_York"),
    (r"\b(PST|PDT|San Francisco|California)\b", "America/Los_Angeles"),
    (r"\b(GST|Dubai|UAE)\b", "Asia/Dubai"),
    (r"\b(SGT|Singapore)\b", "Asia/Singapore"),
]
TLD_TZ = {".in": "Asia/Kolkata", ".co.in": "Asia/Kolkata", ".uk": "Europe/London",
          ".ae": "Asia/Dubai", ".sg": "Asia/Singapore"}


def resolve_timezone(signature_text: str, email: str, contact_tz: str,
                     llm_hint: str, default_tz: str) -> Tuple[str, float]:
    """Returns (tz, confidence). Low confidence means: do not hard-book."""
    if contact_tz:
        return contact_tz, 1.0
    if llm_hint:
        try:
            ZoneInfo(llm_hint)
            return llm_hint, 0.85
        except Exception:
            pass
    for pat, tz in TZ_HINTS:
        if re.search(pat, signature_text or "", re.I):
            return tz, 0.85
    for tld, tz in TLD_TZ.items():
        if (email or "").endswith(tld):
            return tz, 0.75
    return default_tz, 0.45          # below the floor -> router will hold


# ====================================================== the pure slot picker
def pick_slot(now: datetime, busy: List[Tuple[datetime, datetime]], cfg: dict,
              recipient_tz: str, stated_availability: str = "") -> Optional[datetime]:
    """
    Pure. No network, no clock, no randomness. Returns a tz-aware datetime
    in the recipient's zone, or None if nothing fits.

    Rules applied, in order:
      - at least min_lead_hours from now (nobody wants a call in 20 minutes)
      - within max_lead_business_days (a call three weeks out never happens)
      - inside the RECIPIENT's business hours, not ours
      - not on a weekend or a configured holiday
      - not overlapping a busy block, including buffers on both sides
      - if they named a day, only consider that day
    """
    tz = ZoneInfo(recipient_tz)
    dur = timedelta(minutes=cfg["duration_minutes"])
    buf = timedelta(minutes=cfg["buffer_minutes"])
    bh_start, bh_end = cfg["business_hours"]["start"], cfg["business_hours"]["end"]
    holidays = set(cfg.get("holidays", []))

    earliest = now.astimezone(tz) + timedelta(hours=cfg["min_lead_hours"])
    preferred_dow = None
    if stated_availability:
        for day, idx in DAY_NAMES.items():
            if day in stated_availability.lower():
                preferred_dow = idx
                break
    prefer_afternoon = bool(re.search(r"afternoon|after lunch|post lunch",
                                      stated_availability or "", re.I))
    prefer_morning = bool(re.search(r"morning|first thing", stated_availability or "", re.I))

    day = earliest.date()
    business_days_used = 0
    while business_days_used <= cfg["max_lead_business_days"]:
        if day.weekday() >= 5 or day.isoformat() in holidays:
            day += timedelta(days=1)
            continue
        business_days_used += 1

        if preferred_dow is not None and day.weekday() != preferred_dow:
            day += timedelta(days=1)
            continue

        hours = range(bh_start, bh_end)
        if prefer_afternoon:
            hours = list(range(13, bh_end)) + list(range(bh_start, 13))
        elif prefer_morning:
            hours = list(range(bh_start, 13)) + list(range(13, bh_end))

        for hour in hours:
            for minute in (0, 30):
                start = datetime.combine(day, dtime(hour, minute), tzinfo=tz)
                end = start + dur
                if start < earliest:
                    continue
                if end.hour > bh_end or (end.hour == bh_end and end.minute > 0):
                    continue
                clash = any(start - buf < b_end and end + buf > b_start
                            for b_start, b_end in busy)
                if not clash:
                    return start
        day += timedelta(days=1)

    return None


def human_time(dt: datetime, tz_name: str) -> str:
    tzabbr = {"Asia/Kolkata": "IST", "Europe/London": "GMT",
              "America/New_York": "ET", "America/Los_Angeles": "PT",
              "Asia/Dubai": "GST", "Asia/Singapore": "SGT"}.get(tz_name, "")
    end = dt + timedelta(minutes=30)
    def fmt(t):
        h = t.hour % 12 or 12
        return f"{h}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"
    return f"{dt.strftime('%A %d %b')}, {fmt(dt)}-{fmt(end)} {tzabbr}".strip()


# ====================================================== calendar clients
class MockCalendar:
    """Runs with no credentials. Fakes a couple of busy blocks so the
    slot picker has something real to avoid."""
    def __init__(self, seed=7):
        self.rnd = random.Random(seed)
        self.events = {}

    def free_busy(self, start, end, calendar_id="primary"):
        busy = []
        d = start.date()
        while d <= end.date():
            if d.weekday() < 5:
                tz = start.tzinfo
                busy.append((datetime.combine(d, dtime(11, 0), tzinfo=tz),
                             datetime.combine(d, dtime(12, 0), tzinfo=tz)))
                if d.weekday() in (1, 3):
                    busy.append((datetime.combine(d, dtime(15, 0), tzinfo=tz),
                                 datetime.combine(d, dtime(16, 30), tzinfo=tz)))
            d += timedelta(days=1)
        return busy

    def create_event(self, start, tz_name, attendee_email, attendee_name,
                     organiser, thread_key, duration=30, video=True):
        eid = "mock_" + uuid.uuid4().hex[:12]
        self.events[eid] = {"thread_key": thread_key, "start": start}
        return CalendarEvent(
            event_id=eid, start_iso=start.isoformat(),
            end_iso=(start + timedelta(minutes=duration)).isoformat(),
            human_time=human_time(start, tz_name),
            meet_link="https://meet.google.com/mock-abcd-efg" if video else "(phone)",
            timezone=tz_name)

    def cancel(self, event_id):
        self.events.pop(event_id, None)
        return True

    def find_by_thread(self, thread_key):
        for eid, e in self.events.items():
            if e["thread_key"] == thread_key:
                return eid
        return None


class GoogleCalendar:
    """The real thing. Same method names as MockCalendar, so the pipeline
    does not care which one it is holding."""
    def __init__(self, creds_path="token_calendar.json", calendar_id="primary"):
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        self.svc = build("calendar", "v3",
                         credentials=Credentials.from_authorized_user_file(creds_path),
                         cache_discovery=False)
        self.calendar_id = calendar_id

    def free_busy(self, start, end, calendar_id=None):
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                "items": [{"id": calendar_id or self.calendar_id}]}
        r = self.svc.freebusy().query(body=body).execute()
        out = []
        for cal in r.get("calendars", {}).values():
            for b in cal.get("busy", []):
                out.append((datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                            datetime.fromisoformat(b["end"].replace("Z", "+00:00"))))
        return out

    def find_by_thread(self, thread_key):
        """Idempotency. Before creating anything, ask Google whether we
        already made an event for this thread."""
        r = self.svc.events().list(
            calendarId=self.calendar_id,
            privateExtendedProperty=f"cicero_thread={thread_key}",
            singleEvents=True, maxResults=5).execute()
        items = [i for i in r.get("items", []) if i.get("status") != "cancelled"]
        return items[0]["id"] if items else None

    def create_event(self, start, tz_name, attendee_email, attendee_name,
                     organiser, thread_key, duration=30, video=True):
        existing = self.find_by_thread(thread_key)
        if existing:
            ev = self.svc.events().get(calendarId=self.calendar_id,
                                       eventId=existing).execute()
            s = datetime.fromisoformat(ev["start"]["dateTime"])
            return CalendarEvent(event_id=existing, start_iso=ev["start"]["dateTime"],
                                 end_iso=ev["end"]["dateTime"],
                                 human_time=human_time(s, tz_name),
                                 meet_link=ev.get("hangoutLink", ""), timezone=tz_name)

        body = {
            "summary": f"Cicero x {attendee_name}",
            "description": "Intro call. Reply to the email thread to move this.",
            "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": (start + timedelta(minutes=duration)).isoformat(),
                    "timeZone": tz_name},
            "attendees": [{"email": attendee_email}, {"email": organiser}],
            "extendedProperties": {"private": {"cicero_thread": thread_key}},
            "guestsCanModify": True,
            "reminders": {"useDefault": True},
        }
        if video:
            body["conferenceData"] = {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        ev = self.svc.events().insert(
            calendarId=self.calendar_id, body=body,
            conferenceDataVersion=1 if video else 0,
            sendUpdates="all").execute()
        return CalendarEvent(event_id=ev["id"], start_iso=ev["start"]["dateTime"],
                             end_iso=ev["end"]["dateTime"],
                             human_time=human_time(start, tz_name),
                             meet_link=ev.get("hangoutLink", ""), timezone=tz_name)

    def cancel(self, event_id):
        self.svc.events().delete(calendarId=self.calendar_id, eventId=event_id,
                                 sendUpdates="all").execute()
        return True


def get_calendar(mock: bool):
    if mock or not os.path.exists("token_calendar.json"):
        return MockCalendar()
    return GoogleCalendar()
