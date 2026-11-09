"""Conservative Windows meeting signals, never a participant-joining bot."""
from dataclasses import dataclass
import re
import sys
import time


@dataclass(frozen=True)
class Activity:
    source: str
    title: str


NATIVE = {"ms-teams.exe", "teams.exe", "zoom.exe", "webex.exe", "ciscocollabhost.exe"}
BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe"}
MEETING_TITLE = re.compile(r"(Google Meet|meet\.google|Microsoft Teams|Zoom Meeting|Webex|GoTo Meeting)", re.I)
PRESENTATION = re.compile(r"(PowerPoint Slide Show|PowerPoint Presenter View|عرض شرائح)", re.I)


def classify(process: str, title: str, audio_active: bool) -> Activity | None:
    process = process.lower()
    if process == "powerpnt.exe" and PRESENTATION.search(title):
        return Activity("PowerPoint", title)
    if audio_active and (process in NATIVE or process in BROWSERS and MEETING_TITLE.search(title)):
        return Activity(process.removesuffix(".exe"), title or process)
    return None


def windows_activity() -> list[Activity]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    import psutil
    from pycaw.pycaw import AudioUtilities
    titles: dict[int, list[str]] = {}
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            size = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(size + 1)
            user32.GetWindowTextW(hwnd, buf, size + 1)
            if buf.value:
                titles.setdefault(pid.value, []).append(buf.value)
        return True
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows(visit, 0)
    active = set()
    for session in AudioUtilities.GetAllSessions():
        if session.Process and session.State == 1:
            active.add(session.Process.pid)
    found = []
    for pid in set(titles) | active:
        try:
            name = psutil.Process(pid).name()
            for title in titles.get(pid, [""]):
                item = classify(name, title, pid in active)
                if item:
                    found.append(item)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


class MeetingDetector:
    """Debounce start; keep a grace period for silence, mute, and app switches."""
    def __init__(self, grace=90, clock=time.monotonic):
        self.grace = grace
        self.clock = clock
        self.active = False
        self.hits = 0
        self.last_seen = 0.0

    def update(self, activities: list[Activity]) -> str | None:
        now = self.clock()
        if activities:
            self.hits += 1
            self.last_seen = now
            if not self.active and self.hits >= 2:
                self.active = True
                return "start"
        else:
            self.hits = 0
            if self.active and now - self.last_seen >= self.grace:
                self.active = False
                return "stop"
        return None
