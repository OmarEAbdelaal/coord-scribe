"""Upcoming meetings from the Outlook calendar on this PC. Nothing leaves the computer.

Classic Outlook for Windows exposes the signed-in calendar over COM, including
recurring meetings. The "new Outlook" app has no COM interface; a Microsoft Graph
adapter is the planned replacement for it.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import sys

JOIN_URL = re.compile(
    r"https://(?:teams\.microsoft\.com/(?:l/meetup-join|meet)/|teams\.live\.com/meet/|meet\.google\.com/|"
    r"(?:[\w-]+\.)?zoom\.us/(?:j|my|w)/)[^\s<>\"']+", re.I)


class CalendarUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    subject: str
    join_url: str = ""

    @property
    def key(self):
        return f"{self.start.isoformat()}|{self.subject}"


def find_join_url(*texts):
    for text in texts:
        match = JOIN_URL.search(text or "")
        if match:
            return match.group(0).rstrip(".,;)>")
    return ""


def outlook_events(now=None, hours=12):
    """Appointments from now until `hours` ahead. Runs on a worker thread."""
    if sys.platform != "win32":
        return []
    now = now or datetime.now()
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise CalendarUnavailable("Calendar needs the pywin32 package") from exc
    pythoncom.CoInitialize()
    try:
        try:
            namespace = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
            items = namespace.GetDefaultFolder(9).Items  # olFolderCalendar
        except Exception as exc:
            raise CalendarUnavailable("Classic Outlook is not installed or has no signed-in profile") from exc
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        window = (now - timedelta(minutes=30), now + timedelta(hours=hours))
        stamp = "%m/%d/%Y %I:%M %p"
        found = []
        for item in items.Restrict(f"[Start] <= '{window[1].strftime(stamp)}' AND [End] >= '{window[0].strftime(stamp)}'"):
            try:
                start = datetime.fromtimestamp(item.Start.timestamp()) if item.Start.tzinfo else item.Start
                end = datetime.fromtimestamp(item.End.timestamp()) if item.End.tzinfo else item.End
                found.append(Event(start.replace(tzinfo=None), end.replace(tzinfo=None), str(item.Subject or "Meeting"),
                                   find_join_url(str(item.Location or ""), str(item.Body or ""))))
            except Exception:
                continue
            if len(found) >= 50:
                break
        return upcoming(found, now)
    finally:
        pythoncom.CoUninitialize()


def upcoming(events, now):
    """Meetings that have not ended, soonest first, without all-day blocks."""
    return sorted((e for e in events if e.end > now and e.end - e.start < timedelta(hours=12)), key=lambda e: e.start)


def due_reminders(events, now, lead_minutes, notified):
    """Events entering the reminder window that have not been announced yet."""
    due = []
    for event in events:
        if event.key not in notified and event.start - timedelta(minutes=lead_minutes) <= now < event.end:
            notified.add(event.key)
            due.append(event)
    return due


def describe(event, now):
    minutes = round((event.start - now).total_seconds() / 60)
    if minutes <= 0:
        return "in progress"
    if minutes < 60:
        return f"in {minutes} min"
    return event.start.strftime("%H:%M")
