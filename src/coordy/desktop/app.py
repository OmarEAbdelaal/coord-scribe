"""Native APG desktop interface; all capture/model/network work runs off the UI thread."""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
import sys
import threading

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase, QIcon, QPixmap
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..config import load_settings
from ..pipeline import Engine
from ..redact import Redactor
from .captions import CaptionPanel
from .capture import list_audio_devices
from .delivery import Delivery
from .detection import MeetingDetector, windows_activity
from .guest import join_guest, provider_for
from .models import install_models
from .session import CaptureSession
from .settings import DesktopSettings
from .theme import ThemeManager

ASSETS = Path(__file__).parent / "assets"


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


def button(text, action, kind=""):
    item = QPushButton(text)
    item.setObjectName(kind)
    item.clicked.connect(action)
    return item


class Preferences(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Desktop settings")
        self.resize(700, min(760, parent.screen().availableGeometry().height() - 80))
        self.preferences = parent.preferences
        layout = QVBoxLayout(self)
        body = QWidget()
        body.setObjectName("settingsBody")
        body_layout = QVBoxLayout(body)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.controls = {}
        for key, label in [("microphone", "Include my microphone"), ("auto_start", "Automatically capture detected meetings"),
                           ("auto_send", "Send completed minutes automatically")]:
            control = QCheckBox(label)
            control.setChecked(getattr(self.preferences, key))
            self.controls[key] = control
            form.addRow(control)
        for key, label, default in [
            ("speaker_device", "Meeting audio output", "Windows default output"),
            ("microphone_device", "Microphone input", "Windows default microphone"),
        ]:
            control = QComboBox()
            control.setAccessibleName(label)
            control.setMinimumContentsLength(20)
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            control.addItem(default, "")
            saved = getattr(self.preferences, key)
            if saved:
                control.addItem(saved, saved)
                control.setCurrentIndex(1)
            self.controls[key] = control
            form.addRow(label, control)
        self.device_note = QLabel("Computer sound is always captured. Select the speaker or headset your meeting uses; it may differ from the Windows default output.")
        self.device_note.setWordWrap(True)
        self.device_note.setObjectName("muted")
        form.addRow(self.device_note)
        self.refresh_audio = button("Refresh audio devices", self.refresh_devices)
        form.addRow(self.refresh_audio)
        for key, label, options in [("translation", "Live translation", ["none", "Arabic", "English"]),
                                     ("delivery", "Delivery channel", ["teams", "email"])]:
            control = QComboBox()
            control.addItems(options)
            control.setCurrentText(getattr(self.preferences, key))
            self.controls[key] = control
            form.addRow(label, control)
        for key, label in [("auto_stop_seconds", "Detection stop grace (seconds)"), ("smtp_port", "SMTP STARTTLS port")]:
            control = QSpinBox()
            control.setRange(30 if key == "auto_stop_seconds" else 1, 3600 if key == "auto_stop_seconds" else 65535)
            control.setValue(getattr(self.preferences, key))
            self.controls[key] = control
            form.addRow(label, control)
        for key, label in [("smtp_host", "SMTP host"), ("smtp_user", "SMTP username"), ("sender", "Sender email"), ("recipients", "Recipients (comma separated)")]:
            control = QLineEdit(getattr(self.preferences, key))
            self.controls[key] = control
            form.addRow(label, control)
        body_layout.addLayout(form)
        note = QLabel("Delivery credentials come from COORDY_TEAMS_WEBHOOK or COORDY_SMTP_PASSWORD.\nSet these in your Windows user environment before launching.\nOnly redacted minutes are sent. Automatic capture and delivery are opt-in.\nModel and privacy options remain in config.toml.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        body_layout.addWidget(note)
        body_layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)
        if sys.platform == "win32":
            self.refresh_devices()
        else:
            self.refresh_audio.setEnabled(False)

    def refresh_devices(self):
        self.refresh_audio.setEnabled(False)
        self.device_note.setText("Checking Windows audio outputs and microphones…")
        self.parent().spawn(lambda emit: list_audio_devices(), self.populate_devices, self.device_error)

    def populate_devices(self, catalog):
        for key, kind, default in [
            ("speaker_device", "speakers", "Windows default output"),
            ("microphone_device", "microphones", "Windows default microphone"),
        ]:
            control = self.controls[key]
            selected = control.currentData()
            control.clear()
            control.addItem(default, "")
            for name in sorted({device["name"] for device in catalog[kind]}):
                control.addItem(name, name)
            if selected and control.findData(selected) < 0:
                control.addItem(f"Unavailable: {selected}", selected)
            control.setCurrentIndex(max(0, control.findData(selected)))
        self.device_note.setText("Computer sound is always captured. Match Meeting audio output to the speaker/headset selected in Teams, Zoom, or Windows Volume mixer. Restart capture after changing devices.")
        self.refresh_audio.setEnabled(True)

    def device_error(self, message):
        self.device_note.setText("Could not list audio devices: " + message)
        self.refresh_audio.setEnabled(True)

    def accept(self):
        for key, control in self.controls.items():
            value = control.currentData() if key in {"speaker_device", "microphone_device"} else control.isChecked() if isinstance(control, QCheckBox) else control.value() if isinstance(control, QSpinBox) else control.currentText() if isinstance(control, QComboBox) else control.text().strip()
            setattr(self.preferences, key, value)
        super().accept()


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.preferences = DesktopSettings.load(self.settings.paths.root)
        self.workers = set()
        self.busy = False
        self.session = None
        self.current_meeting = ""
        self.guest_stop = None
        self.guest_owns_capture = False
        self.detector = MeetingDetector(self.preferences.auto_stop_seconds)
        self.detecting = False
        self.setWindowTitle("Coord · APG meeting assistant")
        self.setWindowIcon(QIcon(str(ASSETS / "apg-logo.png")))
        self.resize(1240, 820)
        self.setMinimumSize(1000, 700)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(270)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 28, 20, 20)
        self.logo = QLabel()
        self.logo.setObjectName("logo")
        self.logo.setAccessibleName("APG logo")
        original = QPixmap(str(ASSETS / "apg-logo.png"))
        self.logo.setPixmap(original.scaledToWidth(150, Qt.TransformationMode.SmoothTransformation))
        self.logo.setFixedSize(self.logo.sizeHint())
        side.addWidget(self.logo)
        brand = QLabel("Coord")
        brand.setObjectName("brand")
        side.addWidget(brand)
        subtitle = QLabel("Meeting intelligence\nLocal models · Arabic / English")
        subtitle.setObjectName("muted")
        side.addWidget(subtitle)
        side.addSpacing(24)
        side.addWidget(QLabel("Meeting library"))
        self.history = QListWidget()
        self.history.currentItemChanged.connect(self.show_meeting)
        side.addWidget(self.history)
        side.addWidget(button("Settings", self.edit_preferences))
        side.addWidget(button("Open local files", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.paths.root)))))
        outer.addWidget(sidebar)
        content = QVBoxLayout()
        content.setContentsMargins(28, 28, 28, 20)
        content.setSpacing(10)
        outer.addLayout(content, 1)
        heading = QLabel("Meeting workspace")
        heading.setObjectName("heading")
        content.addWidget(heading)
        intro = QLabel("Capture this PC or join as a visible guest. Transcribe locally, review, then share.")
        intro.setObjectName("muted")
        content.addWidget(intro)
        self.badge = QLabel("READY  /  Local processing")
        self.badge.setObjectName("metric")
        self.badge.setAccessibleName("Recording and processing status")
        content.addWidget(self.badge)
        row = QHBoxLayout()
        self.start = button("Start capture", self.start_capture, "primary")
        self.stop = button("Stop and summarise", self.stop_capture, "danger")
        self.stop.setEnabled(False)
        self.import_button = button("Import recording", self.import_file)
        self.models = button("Download models", self.download_models)
        for control in [self.start, self.stop, self.import_button, self.models]:
            row.addWidget(control)
        content.addLayout(row)
        self.monitor = QCheckBox("Detect meetings and presentations on this PC")
        self.monitor.setChecked(self.preferences.auto_start)
        self.monitor.toggled.connect(self.monitor_changed)
        content.addWidget(self.monitor)
        guest = QHBoxLayout()
        self.invite = QLineEdit()
        self.invite.setPlaceholderText("Teams, Google Meet or Zoom invitation URL")
        self.invite.setAccessibleName("Meeting invitation URL")
        self.guest_name = QLineEdit("Coord meeting assistant")
        self.guest_name.setAccessibleName("Visible guest participant name")
        self.guest_name.setMaximumWidth(200)
        self.join = button("Join as guest", self.join_meeting)
        self.leave = button("Leave guest", self.leave_guest)
        self.leave.setEnabled(False)
        for widget in [self.invite, self.guest_name, self.join, self.leave]:
            guest.addWidget(widget)
        content.addLayout(guest)
        self.tabs = QTabWidget()
        self.captions = CaptionPanel(self.preferences.translation)
        self.captions.translation_changed.connect(self.change_caption_translation)
        self.tabs.addTab(self.captions, "Live captions")
        self.transcript = QTextBrowser()
        self.translation = QTextBrowser()
        self.minutes = QTextBrowser()
        for label, browser in [("Live transcript", self.transcript), ("Translation", self.translation), ("Minutes and actions", self.minutes)]:
            browser.setOpenExternalLinks(False)
            self.tabs.addTab(browser, label)
        self.transcript.setPlaceholderText("Arabic and English speech appears here in approximately 10-second chunks.\nSystem audio captures all sounds on the selected Windows output device.")
        self.translation.setPlaceholderText("Choose Arabic or English in Live captions or Settings. Translation uses local Ollama.")
        self.minutes.setPlaceholderText("Stop a recording to generate summary, decisions, actions, risks and open questions.")
        content.addWidget(self.tabs, 1)
        share = QHBoxLayout()
        self.channel = QComboBox()
        self.channel.addItems(["teams", "email"])
        self.channel.setCurrentText(self.preferences.delivery)
        self.send = button("Send minutes", self.send_minutes, "primary")
        self.retry = button("Retry delivery…", self.retry_delivery)
        self.export = button("Export minutes", self.export_minutes)
        for widget in [self.channel, self.send, self.retry, self.export]:
            share.addWidget(widget)
        share.addStretch()
        content.addLayout(share)
        footer = QLabel("Whisper large-v3  /  Ollama local minutes  /  Redacted delivery\nRecording is visible here. Inform participants and follow your meeting policy.")
        footer.setObjectName("muted")
        content.addWidget(footer)
        self.computer_level = QProgressBar()
        self.mic_level = QProgressBar()
        for label, meter in [("Computer sound", self.computer_level), ("Mic", self.mic_level)]:
            meter.setRange(0, 100)
            meter.setValue(0)
            meter.setTextVisible(False)
            meter.setFixedSize(70, 10)
            meter.setAccessibleName(label + " level")
            self.statusBar().addPermanentWidget(QLabel(label))
            self.statusBar().addPermanentWidget(meter)
        self.computer_level.setToolTip("Computer sound from the selected speaker/headset loopback")
        self.mic_level.setToolTip("Optional microphone source")
        self.audio_silent = False
        self.statusBar().showMessage("Ready. Match the meeting output device in Settings.")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_detection)
        self.timer.start(3000)
        self.refresh_history()
        self.update_controls()

    def spawn(self, fn, result=None, error=None):
        worker = Worker(fn)
        self.workers.add(worker)
        worker.event.connect(self.on_event)
        if result:
            worker.result.connect(result)
        worker.failed.connect(error or self.show_error)
        worker.finished.connect(lambda: self.workers.discard(worker))
        worker.start()
        return worker

    def show_error(self, message):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "Coord", message)

    def update_controls(self):
        for widget in [self.import_button, self.models]:
            widget.setEnabled(not self.busy)
        self.start.setEnabled(not self.busy and sys.platform == "win32")
        for widget in [self.send, self.retry, self.export]:
            widget.setEnabled(bool(self.current_meeting) and not self.busy)
        self.stop.setEnabled(self.session is not None)

    def set_busy(self, value):
        self.busy = value
        self.update_controls()

    def on_event(self, kind, value):
        if kind == "audio_devices":
            self.computer_level.setToolTip("Computer sound: " + value["computer"])
            self.mic_level.setToolTip("Microphone: " + (value["microphone"] or "Off"))
            self.mic_level.setEnabled(value["microphone"] is not None)
        elif kind == "audio_levels":
            self.computer_level.setValue(value["computer"])
            self.mic_level.setValue(value["microphone"] or 0)
            silent = value["computer_silent_seconds"] >= 10
            if silent and not self.audio_silent:
                self.statusBar().showMessage("No computer sound for 10s. If the meeting is playing, check Settings > Meeting audio output.")
            elif not silent and self.audio_silent:
                self.statusBar().showMessage("Computer sound received.")
            self.audio_silent = silent
        elif kind == "status":
            self.badge.setText(str(value))
            self.statusBar().showMessage(str(value))
        elif kind == "transcript":
            self.captions.show_segment(value)
            stamp = f"{value['start'] // 60:02.0f}:{value['start'] % 60:02.0f}"
            self.transcript.append(f'<p><span style="font-family:monospace">{stamp} · {html.escape(value["language"])}</span><br><span dir="auto">{html.escape(value["text"])}</span></p>')
        elif kind == "translation":
            self.captions.show_translation(value)
            self.translation.append(f'<p dir="auto">{html.escape(value["language"])} · {html.escape(value["text"])}</p>')
        elif kind == "caption_translation_error":
            self.captions.show_translation_error(value)
        elif kind in {"guest", "guest_joined"}:
            self.statusBar().showMessage(str(value))
            if kind == "guest_joined" and not self.busy and sys.platform == "win32":
                self.guest_owns_capture = True
                self.start_capture()

    def start_capture(self):
        if self.busy:
            return
        self.settings = load_settings()
        self.computer_level.setValue(0)
        self.mic_level.setValue(0)
        self.audio_silent = False
        self.captions.reset()
        self.tabs.setCurrentWidget(self.captions)
        self.transcript.clear()
        self.translation.clear()
        self.minutes.clear()
        self.current_meeting = ""
        self.set_busy(True)
        self.badge.setText("STARTING  /  Recording will begin when audio devices are ready")
        worker = Worker(lambda emit: self.session.run())
        self.session = CaptureSession(self.settings, self.preferences, worker.event.emit)
        self.workers.add(worker)
        worker.event.connect(self.on_event)
        worker.result.connect(self.completed)
        worker.failed.connect(self.capture_failed)
        worker.finished.connect(lambda: self.workers.discard(worker))
        self.update_controls()
        worker.start()

    def stop_capture(self):
        if self.session:
            self.session.stop()
            self.stop.setEnabled(False)
            self.badge.setText("STOPPING  /  Finishing transcript and minutes")

    def capture_failed(self, message):
        self.computer_level.setValue(0)
        self.mic_level.setValue(0)
        self.session = None
        self.set_busy(False)
        self.badge.setText("ATTENTION  /  Processing failed; audio retained locally")
        self.refresh_history()
        self.show_error(message)

    def completed(self, mid):
        self.computer_level.setValue(0)
        self.mic_level.setValue(0)
        self.session = None
        self.current_meeting = mid
        self.set_busy(False)
        self.refresh_history()
        self.load_meeting(mid)
        self.tabs.setCurrentWidget(self.minutes)
        self.badge.setText("COMPLETE  /  Minutes ready")
        if self.preferences.auto_send:
            self.send_minutes()

    def import_file(self):
        file, _ = QFileDialog.getOpenFileName(self, "Import meeting recording", "", "Recordings (*.wav *.mp3 *.m4a *.mp4 *.mkv *.webm *.flac *.ogg *.aac *.wma *.mov)")
        if not file:
            return
        self.settings = load_settings()
        self.set_busy(True)
        self.spawn(lambda emit: Engine(self.settings).process(Path(file), stage=lambda name, frac=None: emit("status", name)),
                   self.completed, self.capture_failed)

    def refresh_history(self):
        self.history.clear()
        engine = Engine(self.settings)
        guard = engine.redactor()
        for meeting in engine.store.meetings(limit=200):
            item = QListWidgetItem(guard.redact(f"{meeting['date']}\n{meeting['title']}\n{meeting['status']}"))
            item.setData(Qt.ItemDataRole.UserRole, meeting["id"])
            self.history.addItem(item)

    def show_meeting(self, item, previous=None):
        if item and not self.busy:
            self.load_meeting(item.data(Qt.ItemDataRole.UserRole))

    def load_meeting(self, mid):
        self.current_meeting = mid
        engine = Engine(self.settings)
        meeting = engine.store.meeting(mid)
        if not meeting:
            return
        path = self.settings.paths.minutes / f"{mid}.md"
        self.minutes.setMarkdown(engine.redactor().redact(path.read_text(encoding="utf-8")) if path.exists() else "Minutes are unavailable. Check local models, then retry processing from the CLI.")
        transcript = self.settings.paths.redacted / f"{mid}.md"
        if transcript.exists():
            self.transcript.setMarkdown(engine.redactor().redact(transcript.read_text(encoding="utf-8")))
        self.translation.clear()
        translations = self.settings.paths.redacted / f"{mid}.translation.json"
        if translations.exists():
            for entry in json.loads(translations.read_text(encoding="utf-8")):
                label = f"{entry["language"]} · " if entry.get("language") else ""
                self.translation.append(html.escape(engine.redactor().redact(label + entry["text"])))
        self.update_controls()

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
            self.captions.target.setCurrentIndex(self.captions.target.findData(self.preferences.translation))
            self.detector.grace = self.preferences.auto_stop_seconds
            self.monitor.setChecked(self.preferences.auto_start)
            self.channel.setCurrentText(self.preferences.delivery)

    def monitor_changed(self, enabled):
        self.preferences.auto_start = enabled
        self.preferences.save(self.settings.paths.root)
        self.detector = MeetingDetector(self.preferences.auto_stop_seconds)

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
        self.spawn(scan, self.detected, failed)

    def detected(self, activities):
        self.detecting = False
        change = self.detector.update(activities)
        if change == "start" and not self.busy:
            self.start_capture()
        elif change == "stop" and self.session:
            self.stop_capture()

    def download_models(self):
        self.settings = load_settings()
        self.set_busy(True)
        def finished(value):
            self.set_busy(False)
            self.badge.setText("MODELS  /  Download completed")
        def failed(message):
            self.set_busy(False)
            self.show_error(message)
        self.spawn(lambda emit: install_models(self.settings, lambda text: emit("status", text)), finished, failed)

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
        channel, mid = self.channel.currentText(), self.current_meeting
        def done(value):
            self.set_busy(False)
            self.statusBar().showMessage(str(value))
        def failed(message):
            self.set_busy(False)
            self.show_error(message)
        self.spawn(lambda emit: Delivery(self.settings.paths.root, self.preferences).send(mid, markdown, channel, retry=retry), done, failed)

    def retry_delivery(self):
        answer = QMessageBox.question(self, "Retry delivery", "Check Teams or the recipient mailbox first. A timed-out delivery may already have arrived. Retry this summary?")
        if answer == QMessageBox.StandardButton.Yes:
            self.send_minutes(retry=True)

    def export_minutes(self):
        try:
            text = self.redacted_minutes()
            target, _ = QFileDialog.getSaveFileName(self, "Export redacted minutes", "meeting-minutes.md", "Markdown (*.md)")
            if target:
                Path(target).write_text(text, encoding="utf-8")
        except Exception as exc:
            self.show_error(str(exc))

    def join_meeting(self):
        try:
            provider_for(self.invite.text())
        except ValueError as exc:
            self.show_error(str(exc))
            return
        url, name = self.invite.text(), self.guest_name.text()
        self.guest_stop = threading.Event()
        self.join.setEnabled(False)
        self.leave.setEnabled(True)
        def finished(value):
            if self.guest_owns_capture:
                self.stop_capture()
                self.guest_owns_capture = False
            self.guest_stop = None
            self.join.setEnabled(True)
            self.leave.setEnabled(False)
            self.statusBar().showMessage(str(value))
        def failed(message):
            finished("Guest could not join")
            self.show_error(message)
        self.spawn(lambda emit: join_guest(url, name, self.guest_stop, emit), finished, failed)

    def leave_guest(self):
        if self.guest_stop:
            self.guest_stop.set()
        self.stop_capture()

    def closeEvent(self, event):
        if self.busy or self.guest_stop or any(w.isRunning() for w in self.workers):
            QMessageBox.information(self, "Coord is working", "Stop capture, leave the guest meeting and wait for processing to finish before closing.")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    for font in ASSETS.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(font))
    theme = ThemeManager(app)
    window = Window()
    window.show()
    if "--smoke-test" in sys.argv:
        # Used by Windows packaging CI: construct the actual frozen interface, then exit.
        QTimer.singleShot(500, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
