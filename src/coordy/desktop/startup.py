"""Start with Windows (per-user Run key) and daily reminders for due actions."""
from datetime import date
from pathlib import Path
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "Coord"


def command():
    """What Windows should launch: the packaged exe, or the module in a dev checkout."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    return f'"{sys.executable}" -m coordy.desktop.app --background'


def set_autostart(enabled):
    if sys.platform != "win32":
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass
    return True


def due_summary(items, today=None):
    """(overdue, due_today) among open actions. Only real ISO dates count."""
    today = today or date.today()
    overdue, due_today = [], []
    for item in items:
        if item["kind"] != "action" or item["status"] in {"closed", "superseded"}:
            continue
        try:
            due = date.fromisoformat(item["due"].strip()[:10])
        except ValueError:
            continue
        if due < today:
            overdue.append(item)
        elif due == today:
            due_today.append(item)
    return overdue, due_today


def reminder_text(overdue, due_today):
    parts = []
    if overdue:
        parts.append(f"{len(overdue)} overdue")
    if due_today:
        parts.append(f"{len(due_today)} due today")
    if not parts:
        return ""
    first = (overdue or due_today)[0]
    return f"{' and '.join(parts)}. First: {first['text'][:90]}"


class ReminderLog:
    """Remember the last day we reminded, so a restart does not repeat it."""
    def __init__(self, root: Path):
        self.path = root / "reminders.txt"

    def due(self, today=None):
        today = (today or date.today()).isoformat()
        return not self.path.exists() or self.path.read_text(encoding="utf-8").strip() != today

    def mark(self, today=None):
        self.path.write_text((today or date.today()).isoformat(), encoding="utf-8")
