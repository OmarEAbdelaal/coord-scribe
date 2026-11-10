"""Native APG desktop interface; all capture/model/network work runs off the UI thread."""
from __future__ import annotations

from datetime import date, datetime
import html
import json
import logging
import shutil
import time
from pathlib import Path
import sys
import threading

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFontDatabase, QIcon, QKeySequence, QPixmap, QShortcut, QTextDocument
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QSplitter, QStackedWidget, QSystemTrayIcon, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..config import load_settings
from ..pipeline import Engine
from ..redact import Redactor
from . import calendar
from .captions import CaptionPanel
from .delivery import Delivery
from .detection import MeetingDetector, windows_activity
from .guest import join_guest, provider_for
from .models import install_models
from .server import apply_server, describe
from .session import CaptureSession
from .settings import DesktopSettings
from .startup import ReminderLog, due_summary, reminder_text, set_autostart
from .theme import ThemeManager
from .theme import LIGHT
from .ui import Card, button, document_html, dot, icon, label, set_state
from .views import ActionsPage, Preferences, SpeakersDialog, overdue

ASSETS = Path(__file__).parent / "assets"
log = logging.getLogger(__name__)

# Database status -> (what the user reads, pill state)
STATUS = {"done": ("Minutes ready", "ok"), "error": ("Needs attention", "error"), "transcribing": ("Transcribing", "busy"),
          "diarizing": ("Detecting speakers", "busy"), "summarising": ("Writing minutes", "busy"), "new": ("New", "busy")}
STAGES = {"decoding audio": "Reading the recording…", "transcribing": "Transcribing speech…",
          "detecting speakers": "Detecting who spoke…", "redacting": "Hiding private names…",
          "extracting decisions and actions": "Writing minutes, decisions and actions…", "done": "Minutes ready"}


class Worker(QThread):
    event = Signal(str, object)
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(self.event.emit))
        except Exception as exc:
            self.failed.emit(str(exc))


def clock_text(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def friendly_date(value):
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return value
    delta = (date.today() - day).days
    return "Today" if delta == 0 else "Yesterday" if delta == 1 else day.strftime("%d %b %Y")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.preferences = DesktopSettings.load(load_settings().paths.root)
        self.settings = self.reload_settings(quiet=True)
        self.workers = set()
        self.background = set()
        self.busy = False
        self.session = None
        self.current_meeting = ""
        self.guest_stop = None
        self.guest_owns_capture = False
        self.detection_owns_capture = False
        self.detector = MeetingDetector(self.preferences.auto_stop_seconds)
        self.detecting = False
        self.events = []
        self.notified = set()
        self.calendar_checked = 0.0
        self.calendar_busy = False
        self.banner_url = ""
        self.quitting = False
        self.tray_hint_shown = False
        self.audio_silent = False
        self.speech_state = {"message": "", "busy": False}
        self.speech_started = 0.0
        self.setWindowTitle("Coord · APG meeting assistant")
        self.setWindowIcon(QIcon(str(ASSETS / "apg-logo.png")))
        self.resize(1320, 860)
        self.setMinimumSize(1080, 700)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.build_sidebar())
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        outer.addLayout(main, 1)
        main.addWidget(self.build_topbar())
        self.banner = self.build_banner()
        main.addWidget(self.banner)
        self.pages = QStackedWidget()
        main.addWidget(self.pages, 1)
        self.pages.addWidget(self.build_home())
        self.pages.addWidget(self.build_meetings())
        self.actions_page = ActionsPage(lambda: Engine(self.settings))
        self.actions_page.changed.connect(self.refresh_stats)
        self.pages.addWidget(self.actions_page)

        self.statusBar().showMessage("Ready. Match the meeting output device in Settings.")
        self.build_tray()
        for keys, action in [("Ctrl+R", self.toggle_capture), ("Ctrl+I", self.import_file), ("Ctrl+,", self.edit_preferences),
                             ("Ctrl+F", self.focus_search), ("Ctrl+1", lambda: self.go(0)), ("Ctrl+2", lambda: self.go(1)),
                             ("Ctrl+3", lambda: self.go(2))]:
            QShortcut(QKeySequence(keys), self, activated=action)
        QApplication.instance().styleHints().colorSchemeChanged.connect(lambda *_: QTimer.singleShot(0, self.refresh_icons))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_detection)
        self.timer.timeout.connect(self.update_speech_progress)
        self.timer.timeout.connect(self.poll_calendar)
        self.timer.timeout.connect(self.poll_action_reminders)
        self.timer.start(3000)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.check_health)
        self.health_timer.start(60000)
        QTimer.singleShot(800, self.check_health)
        self.refresh_icons()
        self.refresh_history()
        self.refresh_stats()
        self.show_events([])
        self.go(0)
        self.update_controls()

    # ---- construction ---------------------------------------------------
    def build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 14)
        side.setSpacing(4)
        brand_row = QHBoxLayout()
        self.logo = QLabel()
        self.logo.setObjectName("logo")
        self.logo.setAccessibleName("APG logo")
        self.logo.setPixmap(QPixmap(str(ASSETS / "apg-logo.png")).scaledToWidth(64, Qt.TransformationMode.SmoothTransformation))
        self.logo.setFixedSize(self.logo.sizeHint())
        brand_row.addWidget(self.logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Coord", "brand"))
        names.addWidget(label("Meeting assistant", "muted"))
        brand_row.addLayout(names)
        brand_row.addStretch()
        side.addLayout(brand_row)
        side.addSpacing(18)
        self.nav = QButtonGroup(self)
        for index, (text, name, tip) in enumerate([("Home", "home", "Today, upcoming meetings and guest (Ctrl+1)"),
                                                   ("Meetings", "meetings", "Live captions, transcripts and minutes (Ctrl+2)"),
                                                   ("Actions", "actions", "Decisions and actions across meetings (Ctrl+3)")]):
            item = button(text, lambda checked=False, i=index: self.go(i), "nav", name, tip)
            item.setCheckable(True)
            self.nav.addButton(item, index)
            side.addWidget(item)
        side.addStretch()
        side.addWidget(label("AI ENGINE", "section"))
        self.speech_health = label("Speech · checking…", "muted", wrap=True)
        self.minutes_health = label("Minutes · checking…", "muted", wrap=True)
        side.addWidget(self.speech_health)
        side.addWidget(self.minutes_health)
        self.models = button("Download models", self.download_models, tip="Download the speech and minutes models once")
        side.addWidget(self.models)
        side.addSpacing(10)
        side.addWidget(button("Settings", self.edit_preferences, "quiet", "settings", "Ctrl+,"))
        side.addWidget(button("Open data folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.paths.root))), "quiet", "folder",
                              "Recordings, transcripts, aliases.toml and config.toml"))
        return sidebar

    def build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(12)
        self.badge = label("Ready", "pill")
        self.badge.setAccessibleName("Recording and processing status")
        self.clock = label("", "clock")
        self.stage = label("Everything stays on your network.", "muted")
        row.addWidget(self.badge)
        row.addWidget(self.clock)
        row.addWidget(self.stage, 1)
        self.computer_level = QProgressBar()
        self.mic_level = QProgressBar()
        for text, meter in [("Meeting", self.computer_level), ("Mic", self.mic_level)]:
            meter.setRange(0, 100)
            meter.setValue(0)
            meter.setTextVisible(False)
            meter.setFixedSize(64, 8)
            meter.setAccessibleName(text + " level")
            row.addWidget(label(text, "muted"))
            row.addWidget(meter)
        self.computer_level.setToolTip("Computer sound from the selected speaker/headset loopback")
        self.mic_level.setToolTip("Optional microphone source")
        self.start = button("Start recording", self.start_capture, "record", "record", "Record this PC's meeting audio (Ctrl+R)")
        self.stop = button("Stop and write minutes", self.stop_capture, "danger", "stop", "Finish and generate minutes (Ctrl+R)")
        self.stop.hide()
        row.addWidget(self.start)
        row.addWidget(self.stop)
        return bar

    def build_banner(self):
        holder = QWidget()
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(24, 12, 24, 0)
        frame = QFrame()
        frame.setObjectName("banner")
        row = QHBoxLayout(frame)
        row.setContentsMargins(16, 10, 10, 10)
        self.banner_text = label("", wrap=True)
        row.addWidget(self.banner_text, 1)
        self.banner_record = button("Record meeting", self.banner_accept, "primary")
        self.banner_guest = button("Send Coord as guest", self.banner_send_guest)
        row.addWidget(self.banner_record)
        row.addWidget(self.banner_guest)
        row.addWidget(button("Not now", self.hide_banner, "quiet"))
        outer.addWidget(frame)
        holder.hide()
        return holder

    def build_home(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(14)
        layout.addWidget(label("Today", "heading"))
        layout.addWidget(label(datetime.now().strftime("%A %d %B %Y") + " · Record a meeting on this PC, or send Coord to listen for you.", "muted"))
        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.stats = {}
        for key, caption in [("meetings", "Meetings recorded"), ("open", "Open actions"), ("late", "Overdue actions"), ("failed", "Need attention")]:
            card = Card()
            value = label("0", "stat")
            card.body.addWidget(value)
            card.body.addWidget(label(caption, "muted"))
            self.stats[key] = value
            stats.addWidget(card)
        layout.addLayout(stats)
        grid = QGridLayout()
        grid.setSpacing(14)

        upcoming = Card("Upcoming meetings", "From Outlook on this PC. Coord reminds you before each meeting and can join for you.")
        self.upcoming = QListWidget()
        self.upcoming.setAccessibleName("Upcoming meetings")
        self.upcoming.itemSelectionChanged.connect(self.update_controls)
        upcoming.body.addWidget(self.upcoming, 1)
        self.calendar_note = label("", "muted", wrap=True)
        upcoming.body.addWidget(self.calendar_note)
        row = QHBoxLayout()
        self.send_guest_to = button("Send guest to selected", self.join_selected_event, "primary", tip="Coord joins this meeting as a visible, listen-only guest")
        row.addWidget(self.send_guest_to)
        row.addWidget(button("Refresh", lambda: self.poll_calendar(force=True)))
        row.addStretch()
        upcoming.body.addLayout(row)
        grid.addWidget(upcoming, 0, 0, 2, 1)

        guest = Card("Send Coord to a meeting", "Coord joins as a visible, listen-only guest and keeps recording even if you leave. The host must admit it.")
        self.invite = QLineEdit()
        self.invite.setPlaceholderText("Paste a Teams, Google Meet or Zoom invitation link")
        self.invite.setAccessibleName("Meeting invitation URL")
        self.invite.setClearButtonEnabled(True)
        self.invite.returnPressed.connect(self.join_meeting)
        guest.body.addWidget(self.invite)
        row = QHBoxLayout()
        self.guest_name = QLineEdit(self.preferences.guest_name)
        self.guest_name.setAccessibleName("Visible guest participant name")
        self.guest_name.setToolTip("Name other participants see")
        self.join = button("Join as guest", self.join_meeting, "primary", "guest")
        self.leave = button("Leave", self.leave_guest)
        self.leave.setEnabled(False)
        row.addWidget(self.guest_name, 1)
        row.addWidget(self.join)
        row.addWidget(self.leave)
        guest.body.addLayout(row)
        self.guest_state = label("", "muted", wrap=True)
        guest.body.addWidget(self.guest_state)
        grid.addWidget(guest, 0, 1)

        capture = Card("Capture on this PC")
        self.monitor = QCheckBox("Notice when a meeting starts and ask me to record")
        self.monitor.setChecked(self.preferences.detect_meetings)
        self.monitor.toggled.connect(self.monitor_changed)
        capture.body.addWidget(self.monitor)
        self.mic_toggle = QCheckBox("Include my microphone in recordings")
        self.mic_toggle.setChecked(self.preferences.microphone)
        self.mic_toggle.toggled.connect(self.microphone_changed)
        capture.body.addWidget(self.mic_toggle)
        row = QHBoxLayout()
        self.import_button = button("Import a recording", self.import_file, "", "import", "Create minutes from an audio or video file (Ctrl+I)")
        row.addWidget(self.import_button)
        row.addStretch()
        capture.body.addLayout(row)
        capture.body.addStretch()
        grid.addWidget(capture, 1, 1)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 4)
        layout.addLayout(grid, 1)
        layout.addWidget(label("Recording is always visible. Inform participants and follow your meeting policy.", "muted"))
        return page

    def build_meetings(self):
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(28, 22, 28, 18)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        outer.addWidget(split)
        library = QWidget()
        side = QVBoxLayout(library)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(10)
        side.addWidget(label("Meetings", "heading"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search titles and transcripts")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh_history)
        side.addWidget(self.search)
        self.history = QListWidget()
        self.history.setAccessibleName("Meeting library")
        self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history.setWordWrap(True)
        self.history.currentItemChanged.connect(self.show_meeting)
        side.addWidget(self.history, 1)
        split.addWidget(library)

        detail = QWidget()
        content = QVBoxLayout(detail)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(8)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = label("No meeting selected", "title")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.meta = label("Start a recording or choose a meeting from the list.", "muted")
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        titles.addWidget(self.title)
        titles.addWidget(self.meta)
        head.addLayout(titles, 1)
        self.chip = label("", "pill")
        self.chip.hide()
        head.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        content.addLayout(head)
        self.tabs = QTabWidget()
        self.captions = CaptionPanel(self.preferences.translation, chunk_seconds=self.preferences.caption_step_seconds)
        self.captions.translation_changed.connect(self.change_caption_translation)
        self.minutes = QTextBrowser()
        self.transcript = QTextBrowser()
        self.translation = QTextBrowser()
        self.tabs.addTab(self.captions, "Live captions")
        for text, browser in [("Minutes", self.minutes), ("Transcript", self.transcript), ("Translation", self.translation)]:
            browser.setOpenExternalLinks(False)
            browser.setObjectName("document")
            self.tabs.addTab(browser, text)
        self.transcript.setPlaceholderText("Arabic and English speech appears here as it is recognised.")
        self.translation.setPlaceholderText("Choose Arabic or English in Live captions. Translation runs on your own AI engine.")
        self.minutes.setPlaceholderText("Stop a recording to generate the summary, decisions, actions, risks and open questions.")
        content.addWidget(self.tabs, 1)
        self.share_row = QWidget()
        share = QHBoxLayout(self.share_row)
        share.setContentsMargins(0, 0, 0, 0)
        self.channel = QComboBox()
        self.channel.setAccessibleName("Delivery channel")
        for text, value in [("Teams", "teams"), ("Email", "email")]:
            self.channel.addItem(text, value)
        self.channel.setCurrentIndex(max(0, self.channel.findData(self.preferences.delivery)))
        self.send = button("Send minutes", self.send_minutes, "primary", tip="Send the redacted minutes to the chosen channel")
        self.export = button("Export", lambda: None)
        menu = QMenu(self.export)
        menu.addAction("Minutes as PDF…", lambda: self.export_minutes("pdf"))
        menu.addAction("Minutes as Markdown…", lambda: self.export_minutes("md"))
        menu.addAction("Minutes with real names (internal)…", lambda: self.export_minutes("named"))
        self.export.setMenu(menu)
        self.speakers = button("Name speakers", self.name_speakers, tip="Match detected voices to people")
        self.more = button("More", lambda: None)
        menu = QMenu(self.more)
        self.retry = QAction("Retry delivery…", self, triggered=self.retry_delivery)
        self.regenerate = QAction("Rewrite minutes", self, triggered=self.regenerate_minutes)
        self.reprocess = QAction("Process recording again", self, triggered=self.reprocess_meeting)
        self.remove = QAction("Delete meeting…", self, triggered=self.delete_meeting)
        menu.addActions([self.retry, self.regenerate, self.reprocess])
        menu.addSeparator()
        menu.addAction(self.remove)
        self.more.setMenu(menu)
        for widget in [self.channel, self.send, self.export, self.speakers]:
            share.addWidget(widget)
        share.addStretch()
        share.addWidget(self.more)
        content.addWidget(self.share_row)
        split.addWidget(detail)
        split.setHandleWidth(18)
        split.setSizes([300, 760])
        split.setStretchFactor(1, 1)
        return page

    def build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Coord meeting assistant")
        menu = QMenu()
        menu.addAction("Open Coord", self.reveal)
        self.tray_record = menu.addAction("Start recording", self.toggle_capture)
        menu.addSeparator()
        menu.addAction("Quit Coord", self.quit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.reveal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.messageClicked.connect(self.reveal)
        self.tray.show()

    def refresh_icons(self):
        """Icons are tinted for the current light/dark palette."""
        for item in self.findChildren(type(self.start)):
            name = item.property("iconName")
            if name:
                tint = "#FFFFFF" if item.objectName() in {"record", "primary"} else "danger" if item.objectName() == "danger" else "muted"
                item.setIcon(icon(name, tint))
        if hasattr(self, "history"):
            self.refresh_history()

    # ---- helpers --------------------------------------------------------
    def reload_settings(self, quiet=False):
        settings = load_settings()
        try:
            apply_server(settings, self.preferences)
        except ValueError as exc:
            if not quiet:
                raise
            log.warning("AI server ignored: %s", exc)
        self.settings = settings
        return settings

    def go(self, index):
        self.pages.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)
        if index == 2:
            self.actions_page.refresh()

    def focus_search(self):
        self.go(1)
        self.search.setFocus()

    def reveal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self):
        self.quitting = True
        self.close()

    def notify(self, title, text):
        self.statusBar().showMessage(f"{title}: {text}")
        if self.tray:
            self.tray.showMessage(title, text, self.windowIcon(), 10000)
        QApplication.alert(self)

    def set_status(self, state, text, detail=None):
        set_state(self.badge, state, text)
        if detail is not None:
            self.stage.setText(detail)

    def spawn(self, fn, result=None, error=None, background=False):
        worker = Worker(fn)
        owners = self.background if background else self.workers
        owners.add(worker)
        worker.event.connect(self.on_event)
        if result:
            worker.result.connect(result)
        worker.failed.connect(error or self.show_error)
        worker.finished.connect(lambda: owners.discard(worker))
        worker.start()
        return worker

    def show_error(self, message):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "Coord", message)

    def update_controls(self):
        recording = self.session is not None
        for widget in [self.import_button, self.models]:
            widget.setEnabled(not self.busy)
        self.start.setEnabled(not self.busy and sys.platform == "win32")
        self.start.setToolTip("Record this PC's meeting audio (Ctrl+R)" if sys.platform == "win32" else
                              "Live recording needs Windows. Import a recording instead.")
        self.start.setVisible(not recording)
        self.stop.setVisible(recording)
        self.stop.setEnabled(recording)
        has_meeting = bool(self.current_meeting) and not self.busy
        meeting = Engine(self.settings).store.meeting(self.current_meeting) if has_meeting else None
        has_minutes = bool(meeting) and (self.settings.paths.minutes / f"{self.current_meeting}.md").is_file()
        for widget in [self.send, self.export, self.channel]:
            widget.setEnabled(has_minutes)
        self.speakers.setEnabled(has_minutes)
        self.more.setEnabled(bool(meeting))
        self.retry.setEnabled(has_minutes)
        self.regenerate.setEnabled(bool(meeting) and (self.settings.paths.raw / f"{self.current_meeting}.json").is_file())
        self.reprocess.setEnabled(bool(meeting) and Path(meeting["audio_path"]).is_file())
        self.history.setEnabled(not self.busy)
        self.share_row.setVisible(not recording)
        self.send_guest_to.setEnabled(self.guest_stop is None and bool(self.selected_event() and self.selected_event().join_url))
        if self.tray:
            self.tray_record.setText("Stop and write minutes" if recording else "Start recording")
            self.tray_record.setEnabled(recording or self.start.isEnabled())

    def set_busy(self, value):
        self.busy = value
        self.update_controls()

    def update_speech_progress(self):
        if self.speech_state["message"]:
            message = self.speech_state["message"]
            if self.speech_state["busy"]:
                elapsed = int(time.monotonic() - self.speech_started)
                message += f" · {elapsed}s elapsed"
            self.captions.notice.setText(message)

    # ---- events from workers ---------------------------------------------
    def on_event(self, kind, value):
        if kind == "speech_state":
            if value["busy"] and not self.speech_state["busy"] or not value["busy"]:
                self.speech_started = time.monotonic()
            self.speech_state = value
            self.update_speech_progress()
        elif kind == "audio_devices":
            self.computer_level.setToolTip("Computer sound: " + value["computer"])
            self.mic_level.setToolTip("Microphone: " + (value["microphone"] or "Off"))
            self.mic_level.setEnabled(value["microphone"] is not None)
        elif kind == "audio_levels":
            if "recorded_seconds" in value and self.session and self.stop.isEnabled():
                self.set_status("rec", "● Recording")
                self.clock.setText(clock_text(value["recorded_seconds"]))
            self.computer_level.setValue(value["computer"])
            self.mic_level.setValue(value["microphone"] or 0)
            silent = value["computer_silent_seconds"] >= 10
            if silent and not self.audio_silent:
                self.statusBar().showMessage("No computer sound for 10s. If the meeting is playing, check Settings > Meeting audio output.")
            elif not silent and self.audio_silent:
                self.statusBar().showMessage("Computer sound received.")
            self.audio_silent = silent
        elif kind == "status":
            text = STAGES.get(str(value), str(value))
            self.stage.setText(text)
            self.statusBar().showMessage(text)
        elif kind == "partial":
            self.captions.show_partial(value)
        elif kind == "transcript":
            self.captions.show_segment(value)
            stamp = f"{value['start'] // 60:02.0f}:{value['start'] % 60:02.0f}"
            self.transcript.append(f'<p><span style="font-family:monospace">{stamp} · {html.escape(value["language"])}</span><br><span dir="auto">{html.escape(value["text"])}</span></p>')
        elif kind == "translation":
            self.captions.show_translation(value)
            self.translation.append(f'<p dir="auto">{html.escape(value["language"])} · {html.escape(value["text"])}</p>')
        elif kind == "translation_state":
            self.captions.translation_status.setText(str(value))
        elif kind == "caption_translation_error":
            self.captions.show_translation_error(value)
        elif kind in {"guest", "guest_joined"}:
            self.statusBar().showMessage(str(value))
            self.guest_state.setText(str(value))
            if kind == "guest_joined" and not self.busy and sys.platform == "win32":
                self.guest_owns_capture = True
                self.start_capture()

    # ---- capture ----------------------------------------------------------
    def toggle_capture(self):
        if self.session:
            self.stop_capture()
        elif self.start.isEnabled():
            self.start_capture()

    def start_capture(self, checked=False):
        if self.busy:
            return
        try:
            self.reload_settings()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.hide_banner()
        self.computer_level.setValue(0)
        self.mic_level.setValue(0)
        self.audio_silent = False
        self.speech_state = {"message": "Recording. Captions appear about a second after someone speaks.", "busy": False}
        self.update_speech_progress()
        self.captions.reset()
        self.go(1)
        self.tabs.setCurrentWidget(self.captions)
        self.transcript.clear()
        self.translation.clear()
        self.minutes.clear()
        self.current_meeting = ""
        self.history.blockSignals(True)
        self.history.setCurrentRow(-1)
        self.history.blockSignals(False)
        self.title.setText("Live meeting")
        self.meta.setText(datetime.now().strftime("Started %H:%M") + " · minutes are written when you stop")
        self.chip.hide()
        self.clock.setText("00:00:00")
        self.set_busy(True)
        self.set_status("busy", "Starting", "Opening audio devices…")
        worker = Worker(lambda emit: self.session.run())
        try:
            self.session = CaptureSession(self.settings, self.preferences, worker.event.emit)
        except Exception as exc:
            self.capture_failed(str(exc))
            return
        self.workers.add(worker)
        worker.event.connect(self.on_event)
        worker.result.connect(self.completed)
        worker.failed.connect(self.capture_failed)
        worker.finished.connect(lambda: self.workers.discard(worker))
        self.update_controls()
        worker.start()

    def stop_capture(self, checked=False, *, reason="user request"):
        if self.session:
            self.session.stop(reason)
            self.stop.setEnabled(False)
            self.set_status("busy", "Finishing", "Finishing the transcript and writing minutes…")

    def finish(self):
        self.computer_level.setValue(0)
        self.mic_level.setValue(0)
        self.clock.setText("")
        self.session = None
        self.detection_owns_capture = False
        self.captions.show_partial({"text": ""})

    def capture_failed(self, message):
        self.finish()
        self.set_busy(False)
        self.speech_state = {"message": "Processing failed. Check the error message or logs/desktop.log.", "busy": False}
        self.update_speech_progress()
        self.set_status("error", "Needs attention", "Processing failed; the recording is kept on this PC.")
        self.refresh_history()
        self.refresh_stats()
        self.show_error(message)

    def completed(self, mid):
        self.finish()
        self.current_meeting = mid
        self.set_busy(False)
        self.speech_state = {"message": "Minutes ready.", "busy": False}
        self.update_speech_progress()
        self.refresh_history()
        self.refresh_stats()
        self.go(1)
        self.load_meeting(mid)
        self.tabs.setCurrentWidget(self.minutes)
        self.set_status("ok", "Minutes ready", "Review the minutes, then send or export them.")
        if not self.isActiveWindow():
            self.notify("Minutes ready", self.title.text())
        if self.preferences.auto_send:
            self.send_minutes()

    def process_file(self, path, **kwargs):
        try:
            self.reload_settings()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.set_busy(True)
        self.set_status("busy", "Processing", "Reading the recording…")
        self.spawn(lambda emit: Engine(self.settings).process(Path(path), stage=lambda name, frac=None: emit("status", name), **kwargs),
                   self.completed, self.capture_failed)

    def import_file(self):
        if self.busy:
            return
        file, _ = QFileDialog.getOpenFileName(self, "Import meeting recording", "", "Recordings (*.wav *.mp3 *.m4a *.mp4 *.mkv *.webm *.flac *.ogg *.aac *.wma *.mov)")
        if file:
            self.process_file(file)

    # ---- library ------------------------------------------------------------
    def refresh_history(self, *_):
        engine = Engine(self.settings)
        guard = engine.redactor()
        query = self.search.text().strip().lower()
        matches = {hit["meeting_id"] for hit in engine.store.search_segments(query, limit=60)} if len(query) >= 3 else set()
        self.history.blockSignals(True)
        self.history.clear()
        for meeting in engine.store.meetings(limit=300):
            title = guard.redact(meeting["title"])
            if query and query not in title.lower() and meeting["id"] not in matches:
                continue
            text, state = STATUS.get(meeting["status"], (meeting["status"], "busy"))
            length = f" · {max(1, round(meeting['duration'] / 60))} min" if meeting["duration"] else ""
            item = QListWidgetItem(dot({"ok": "ok", "error": "danger"}.get(state, "link")),
                                   f"{title}\n{friendly_date(meeting['date'])}{length} · {text}")
            item.setData(Qt.ItemDataRole.UserRole, meeting["id"])
            item.setToolTip(guard.redact(meeting["error"]) if meeting["status"] == "error" else title)
            self.history.addItem(item)
            if meeting["id"] == self.current_meeting:
                self.history.setCurrentItem(item)
        self.history.blockSignals(False)

    def refresh_stats(self):
        store = Engine(self.settings).store
        meetings = store.meetings(limit=1000)
        actions = [i for i in store.items(kind="action", limit=2000) if i["status"] not in {"closed", "superseded"}]
        values = {"meetings": sum(m["status"] == "done" for m in meetings), "open": len(actions),
                  "late": sum(overdue(i) for i in actions), "failed": sum(m["status"] == "error" for m in meetings)}
        for key, value in values.items():
            self.stats[key].setText(str(value))

    def show_meeting(self, item, previous=None):
        if item and not self.busy:
            self.load_meeting(item.data(Qt.ItemDataRole.UserRole))

    def load_meeting(self, mid):
        self.current_meeting = mid
        engine = Engine(self.settings)
        meeting = engine.store.meeting(mid)
        if not meeting:
            return
        guard = engine.redactor()
        text, state = STATUS.get(meeting["status"], (meeting["status"], "busy"))
        self.title.setText(guard.redact(meeting["title"]) or mid)
        details = [friendly_date(meeting["date"])]
        if meeting["duration"]:
            details.append(f"{max(1, round(meeting['duration'] / 60))} min")
        if meeting["language"]:
            details.append(meeting["language"].replace(",", " + "))
        if meeting["project"]:
            details.append(meeting["project"])
        self.meta.setText(" · ".join(details))
        set_state(self.chip, state, text)
        self.chip.show()
        path = self.settings.paths.minutes / f"{mid}.md"
        if path.exists():
            self.minutes.setHtml(document_html(guard.redact(path.read_text(encoding="utf-8"))))
            self.tabs.setCurrentWidget(self.minutes)
        elif meeting["status"] == "error":
            self.minutes.setPlainText("This meeting could not be processed.\n\nReason: " + guard.redact(meeting["error"] or "unknown") +
                                      "\n\nThe recording is kept on this PC. Fix the problem, then choose More › Process recording again.")
            self.tabs.setCurrentWidget(self.minutes)
        else:
            self.minutes.setPlainText("Meeting status: " + meeting["status"] + "\n\nNo minutes generated yet.")
        self.transcript.clear()
        transcript = self.settings.paths.redacted / f"{mid}.md"
        if transcript.exists():
            self.transcript.setMarkdown(guard.redact(transcript.read_text(encoding="utf-8")))
        self.translation.clear()
        translations = self.settings.paths.redacted / f"{mid}.translation.json"
        if translations.exists():
            for entry in json.loads(translations.read_text(encoding="utf-8")):
                prefix = f"{entry['language']} · " if entry.get("language") else ""
                self.translation.append(html.escape(guard.redact(prefix + entry["text"])))
        self.update_controls()

    def name_speakers(self):
        engine = Engine(self.settings)
        if SpeakersDialog(self, engine, self.current_meeting).exec() == QDialog.DialogCode.Accepted:
            self.load_meeting(self.current_meeting)
            self.statusBar().showMessage("Speakers saved. Choose More › Rewrite minutes to apply them to the minutes.")

    def regenerate_minutes(self):
        mid = self.current_meeting
        self.set_busy(True)
        self.set_status("busy", "Processing", "Rewriting minutes…")
        def work(emit):
            engine = Engine(self.settings)
            engine.redact(mid)
            engine.summarise(mid, stage=lambda name, frac=None: emit("status", name))
            return mid
        self.spawn(work, self.completed, self.capture_failed)

    def reprocess_meeting(self):
        engine = Engine(self.settings)
        meeting = engine.store.meeting(self.current_meeting)
        engine.delete_meeting(meeting["id"])
        self.current_meeting = ""
        self.process_file(meeting["audio_path"], title=meeting["title"], project=meeting["project"], day=meeting["date"])

    def delete_meeting(self):
        answer = QMessageBox.question(self, "Delete meeting", "Remove this meeting, its transcript and minutes from Coord?\nThe audio recording stays in the data folder.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        Engine(self.settings).delete_meeting(self.current_meeting)
        self.current_meeting = ""
        self.title.setText("No meeting selected")
        self.meta.setText("Start a recording or choose a meeting from the list.")
        self.chip.hide()
        for view in [self.minutes, self.transcript, self.translation]:
            view.clear()
        self.refresh_history()
        self.refresh_stats()
        self.update_controls()

    # ---- settings -------------------------------------------------------------
    def change_caption_translation(self, language):
        self.preferences.translation = language
        self.preferences.save(self.settings.paths.root)
        if self.session:
            self.session.set_translation(language)

    def edit_preferences(self):
        if self.busy:
            self.show_error("Stop capture and wait for processing before changing settings.")
            return
        if Preferences(self).exec() == QDialog.DialogCode.Accepted:
            self.preferences.save(self.settings.paths.root)
            self.reload_settings(quiet=True)
            try:
                set_autostart(self.preferences.start_with_windows)
            except OSError as exc:
                self.show_error(f"Could not change start with Windows: {exc}")
            self.captions.target.setCurrentIndex(self.captions.target.findData(self.preferences.translation))
            self.captions.notice.setText(CaptionPanel.idle_notice(self.preferences.caption_step_seconds))
            self.detector.grace = self.preferences.auto_stop_seconds
            self.monitor.setChecked(self.preferences.detect_meetings)
            self.mic_toggle.setChecked(self.preferences.microphone)
            self.guest_name.setText(self.preferences.guest_name)
            self.channel.setCurrentIndex(max(0, self.channel.findData(self.preferences.delivery)))
            self.check_health()
            self.poll_calendar(force=True)

    def monitor_changed(self, enabled):
        self.preferences.detect_meetings = enabled
        self.preferences.save(self.settings.paths.root)
        self.detector = MeetingDetector(self.preferences.auto_stop_seconds)
        if not enabled:
            self.hide_banner()

    def microphone_changed(self, enabled):
        self.preferences.microphone = enabled
        self.preferences.save(self.settings.paths.root)

    # ---- AI engine health -------------------------------------------------------
    def check_health(self):
        if self.busy or not self.isVisible() and not self.tray:
            return
        settings = self.settings
        def probe(emit):
            from ..gpu import cuda_device_count
            from ..llm import Ollama
            speech_where, minutes_where = describe(settings)
            report = {"speech": f"{settings.whisper.server_model or settings.whisper.model} · {speech_where}",
                      "minutes": f"{settings.llm.model} · {minutes_where}", "speech_ok": True, "minutes_ok": False,
                      "minutes_hint": ""}
            if not settings.whisper.server:
                report["speech"] += " · GPU" if cuda_device_count() > 0 else " · CPU (slow)"
                report["speech_ok"] = cuda_device_count() > 0
            try:
                client = Ollama(settings.llm, settings.privacy)
                try:
                    client.version()
                    report["minutes_ok"] = client.has_model()
                    report["minutes_hint"] = "" if report["minutes_ok"] else "model not downloaded"
                finally:
                    client.close()
            except Exception:
                report["minutes_hint"] = "not reachable"
            return report
        self.spawn(probe, self.show_health, lambda message: None, background=True)

    def show_health(self, report):
        mark = lambda ok: "●" if ok else "▲"
        self.speech_health.setText(f"{mark(report['speech_ok'])} Speech\n{report['speech']}")
        self.speech_health.setObjectName("dotOk" if report["speech_ok"] else "dotWarn")
        hint = f" · {report['minutes_hint']}" if report["minutes_hint"] else ""
        self.minutes_health.setText(f"{mark(report['minutes_ok'])} Minutes\n{report['minutes']}{hint}")
        self.minutes_health.setObjectName("dotOk" if report["minutes_ok"] else "dotError")
        for widget in [self.speech_health, self.minutes_health]:
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.models.setVisible(not report["minutes_ok"] or not self.settings.whisper.server)
        self.models.setText("Download models" if not report["minutes_ok"] else "Check models")
        self.models.setObjectName("" if report["minutes_ok"] else "primary")
        self.models.style().unpolish(self.models)
        self.models.style().polish(self.models)

    def download_models(self):
        try:
            self.reload_settings()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.set_busy(True)
        self.set_status("busy", "Downloading", "Downloading models…")
        def finished(value):
            self.set_busy(False)
            self.set_status("ok", "Models ready", "Speech and minutes models are installed.")
            self.check_health()
        def failed(message):
            self.set_busy(False)
            self.set_status("error", "Needs attention", "Model download failed.")
            self.show_error(message)
        self.spawn(lambda emit: install_models(self.settings, lambda text: emit("status", text)), finished, failed)

    # ---- detection, calendar and the prompt banner --------------------------------
    def show_banner(self, text, url="", record=True):
        self.banner_text.setText(text)
        self.banner_url = url
        self.banner_record.setVisible(record and sys.platform == "win32")
        self.banner_guest.setVisible(bool(url) and self.guest_stop is None)
        self.banner.show()

    def hide_banner(self):
        self.banner.hide()

    def banner_accept(self):
        self.detection_owns_capture = self.detector.active
        self.start_capture()

    def banner_send_guest(self):
        self.invite.setText(self.banner_url)
        self.hide_banner()
        self.join_meeting()

    def poll_detection(self):
        if not self.monitor.isChecked() or self.detecting or sys.platform != "win32":
            return
        self.detecting = True
        def scan(emit):
            import comtypes
            comtypes.CoInitialize()
            try:
                return windows_activity()
            finally:
                comtypes.CoUninitialize()
        def failed(message):
            self.detecting = False
            self.monitor.setChecked(False)
            self.show_error("Meeting detection stopped: " + message)
        self.spawn(scan, self.detected, failed, background=True)

    def detected(self, activities):
        self.detecting = False
        change = self.detector.update(activities)
        if change == "start" and not self.busy:
            source = activities[0].source.replace("ms-teams", "Teams").title()
            if self.preferences.auto_start:
                self.detection_owns_capture = True
                self.start_capture()
                self.notify("Recording started", f"{source} meeting detected. Coord is recording and will stop when it ends.")
            else:
                self.show_banner(f"<b>{html.escape(source)} meeting detected.</b> Record it? Recording stops by itself when the meeting ends.")
                self.notify("Meeting detected", f"{source} is in a meeting. Open Coord to record it.")
        elif change == "stop":
            self.hide_banner()
            if self.session and self.detection_owns_capture:
                self.stop_capture(reason="meeting detection inactivity")

    def poll_calendar(self, force=False):
        if not self.preferences.calendar or sys.platform != "win32":
            return
        now = datetime.now()
        for event in calendar.due_reminders(self.events, now, self.preferences.reminder_minutes, self.notified):
            when = calendar.describe(event, now)
            self.notify("Meeting " + ("starting" if when == "in progress" else when), event.subject)
            if self.preferences.auto_join_guest and event.join_url and self.guest_stop is None and not self.busy:
                self.invite.setText(event.join_url)
                self.join_meeting()
            elif not self.busy:
                self.show_banner(f"<b>{html.escape(event.subject)}</b> · {when}. Record it on this PC, or send Coord to listen for you.",
                                 url=event.join_url)
        if self.calendar_busy or not force and time.monotonic() - self.calendar_checked < 300:
            self.show_events(self.events)
            return
        self.calendar_busy = True
        self.calendar_checked = time.monotonic()
        def done(events):
            self.calendar_busy = False
            self.calendar_error = ""
            self.show_events(events)
        def failed(message):
            self.calendar_busy = False
            self.calendar_error = message
            self.show_events([])
        self.spawn(lambda emit: calendar.outlook_events(), done, failed, background=True)

    def poll_action_reminders(self):
        if not self.preferences.action_reminders:
            return
        log_file = ReminderLog(self.settings.paths.root)
        if not log_file.due():
            return
        overdue, due_today = due_summary(Engine(self.settings).store.items(kind="action", limit=2000))
        log_file.mark()
        text = reminder_text(overdue, due_today)
        if text:
            self.notify("Actions need attention", text)

    def show_events(self, events):
        now = datetime.now()
        self.events = calendar.upcoming(events, now)
        selected = self.selected_event()
        self.upcoming.clear()
        for event in self.events:
            try:
                provider = provider_for(event.join_url) if event.join_url else "No online link"
            except ValueError:
                provider = "Online"
            item = QListWidgetItem(dot("ok" if event.start <= now else "link"),
                                   f"{event.start:%H:%M} – {event.end:%H:%M}   {event.subject}\n{provider} · {calendar.describe(event, now)}")
            item.setData(Qt.ItemDataRole.UserRole, event.key)
            self.upcoming.addItem(item)
            if selected and selected.key == event.key:
                self.upcoming.setCurrentItem(item)
        if self.events:
            note = ""
        elif sys.platform != "win32":
            note = "Calendar and live recording need Windows. You can still import recordings here."
        elif not self.preferences.calendar:
            note = "Calendar is switched off in Settings › Meetings."
        elif getattr(self, "calendar_error", ""):
            note = self.calendar_error + ". Paste a meeting link on the right instead."
        else:
            note = "No more meetings in the next 12 hours."
        self.calendar_note.setText(note)
        self.calendar_note.setVisible(bool(note))
        self.update_controls()

    def selected_event(self):
        item = self.upcoming.currentItem()
        key = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((event for event in self.events if event.key == key), None)

    def join_selected_event(self):
        event = self.selected_event()
        if event and event.join_url:
            self.invite.setText(event.join_url)
            self.join_meeting()

    # ---- sharing ----------------------------------------------------------------
    def redacted_minutes(self):
        path = self.settings.paths.minutes / f"{self.current_meeting}.md"
        if not path.is_file():
            raise ValueError("This meeting has no minutes to send")
        return Redactor.from_settings(self.settings).redact(path.read_text(encoding="utf-8"))

    def send_minutes(self, checked=False, *, retry=False):
        try:
            markdown = self.redacted_minutes()
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.set_busy(True)
        channel, mid = self.channel.currentData(), self.current_meeting
        def done(value):
            self.set_busy(False)
            self.statusBar().showMessage(f"{value} · {self.channel.currentText()}")
        def failed(message):
            self.set_busy(False)
            self.show_error(message)
        self.spawn(lambda emit: Delivery(self.settings.paths.root, self.preferences).send(mid, markdown, channel, retry=retry), done, failed)

    def retry_delivery(self):
        answer = QMessageBox.question(self, "Retry delivery", "Check Teams or the recipient mailbox first. A timed-out delivery may already have arrived. Retry this summary?")
        if answer == QMessageBox.StandardButton.Yes:
            self.send_minutes(retry=True)

    def export_minutes(self, kind="md", target=""):
        try:
            text = self.redacted_minutes()
            name = self.current_meeting or "meeting-minutes"
            if kind == "named":
                answer = QMessageBox.question(self, "Export with real names", "This file restores the real names of people, companies and projects. Keep it inside the company. Continue?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
                source = Engine(self.settings).write_named_minutes(self.current_meeting)
                target = target or QFileDialog.getSaveFileName(self, "Export minutes with real names", f"{name}.named.md", "Markdown (*.md)")[0]
                if target:
                    shutil.copyfile(source, target)
            elif kind == "pdf":
                target = target or QFileDialog.getSaveFileName(self, "Export redacted minutes", f"{name}.pdf", "PDF (*.pdf)")[0]
                if target:
                    write_pdf(text, target)
            else:
                target = target or QFileDialog.getSaveFileName(self, "Export redacted minutes", f"{name}.md", "Markdown (*.md)")[0]
                if target:
                    Path(target).write_text(text, encoding="utf-8")
            if target:
                self.statusBar().showMessage(f"Exported to {target}")
        except Exception as exc:
            self.show_error(str(exc))

    # ---- guest ------------------------------------------------------------------
    def join_meeting(self):
        if self.guest_stop is not None:
            return
        try:
            provider_for(self.invite.text().strip())
        except ValueError as exc:
            self.show_error(str(exc))
            return
        url, name = self.invite.text().strip(), self.guest_name.text().strip()
        if name and name != self.preferences.guest_name:
            self.preferences.guest_name = name
            self.preferences.save(self.settings.paths.root)
        self.guest_stop = threading.Event()
        self.join.setEnabled(False)
        self.leave.setEnabled(True)
        self.guest_state.setText("Opening the guest browser…")
        self.update_controls()
        def finished(value):
            if self.guest_owns_capture:
                self.stop_capture()
                self.guest_owns_capture = False
            self.guest_stop = None
            self.join.setEnabled(True)
            self.leave.setEnabled(False)
            self.guest_state.setText(str(value))
            self.statusBar().showMessage(str(value))
            self.update_controls()
        def failed(message):
            finished("Guest could not join")
            self.show_error(message)
        self.spawn(lambda emit: join_guest(url, name, self.guest_stop, emit), finished, failed)

    def leave_guest(self):
        if self.guest_stop:
            self.guest_stop.set()
        self.stop_capture()

    def closeEvent(self, event):
        if not self.quitting and self.tray and self.preferences.close_to_tray:
            # Keep noticing meetings and reminders in the background.
            event.ignore()
            self.hide()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self.tray.showMessage("Coord is still running", "It keeps watching for meetings. Right-click the icon to quit.", self.windowIcon(), 6000)
            return
        if self.busy or self.guest_stop or any(w.isRunning() for w in self.workers):
            self.quitting = False
            QMessageBox.information(self, "Coord is working", "Stop capture, leave the guest meeting and wait for processing to finish before closing.")
            event.ignore()
            return
        self.timer.stop()
        self.health_timer.stop()
        for worker in list(self.background):
            worker.wait(3000)
        if self.tray:
            self.tray.hide()
        event.accept()
        if self.quitting:
            QApplication.instance().quit()


def write_pdf(markdown, target):
    """Print minutes as an A4 PDF in dark text, whatever the screen theme is."""
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter
    document = QTextDocument()
    document.setDefaultFont(QApplication.instance().font())
    document.setHtml(document_html(markdown, LIGHT))
    writer = QPdfWriter(str(target))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(16, 16, 16, 16), QPageLayout.Unit.Millimeter)
    writer.setTitle("Minutes of meeting")
    document.print_(writer)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Coord")
    app.setQuitOnLastWindowClosed(False)
    for font in ASSETS.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font))
    theme = ThemeManager(app)
    window = Window()
    handlers = [logging.FileHandler(window.settings.paths.logs / "desktop.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("coordy.transcribe").setLevel(logging.INFO)
    logging.getLogger("coordy.desktop.capture").setLevel(logging.INFO)
    logging.getLogger("coordy.desktop.session").setLevel(logging.INFO)
    if "--background" in sys.argv and window.tray:
        window.hide()  # started by Windows sign-in: live in the tray
    else:
        window.show()
    if "--smoke-test" in sys.argv:
        # Used by Windows packaging CI: construct the actual frozen interface, then exit.
        QTimer.singleShot(500, app.quit)
    elif not window.tray:
        app.setQuitOnLastWindowClosed(True)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
