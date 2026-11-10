"""Secondary screens: settings, action register, speaker naming and agenda drafts."""
from __future__ import annotations

from datetime import date
import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QSpinBox, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .capture import list_audio_devices
from .server import apply_server
from .ui import button, label

KINDS = [("Actions", "action"), ("Decisions", "decision"), ("Risks", "risk"), ("Open questions", "question"), ("Everything", "")]
KIND_LABEL = {"action": "Action", "decision": "Decision", "risk": "Risk", "question": "Question"}


def overdue(item, today=None):
    """True for an open item whose due date is a real ISO date in the past."""
    if item["status"] in {"closed", "superseded"}:
        return False
    try:
        return date.fromisoformat(item["due"].strip()[:10]) < (today or date.today())
    except ValueError:
        return False


def draft_agenda(items, title="Next coordination meeting"):
    """Agenda built from what is still open. No model needed, so it always works."""
    open_items = [i for i in items if i["status"] not in {"closed", "superseded"}]
    lines = [f"# Agenda: {title}", "", f"Prepared {date.today().isoformat()} from the Coord register.", ""]
    sections = [("action", "1. Review of open actions"), ("question", "2. Open questions and RFIs"),
                ("risk", "3. Risks to review")]
    for kind, heading in sections:
        rows = [i for i in open_items if i["kind"] == kind]
        lines += [f"## {heading}", ""]
        if not rows:
            lines += ["- Nothing open", ""]
            continue
        for row in rows:
            detail = ", ".join(part for part in [row["owner"] and f"owner {row['owner']}", row["due"] and f"due {row['due']}",
                                                 "OVERDUE" if overdue(row) else "", row["meeting_date"]] if part)
            lines.append(f"- {row['text']}" + (f" ({detail})" if detail else ""))
        lines.append("")
    lines += ["## 4. New items", "", "- ", "", "## 5. Any other business and next meeting", "", "- ", ""]
    return "\n".join(lines)


class TextDialog(QDialog):
    """Read-only Markdown with copy and save, used for agenda drafts."""
    def __init__(self, parent, title, markdown, filename):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 640)
        self.markdown, self.filename = markdown, filename
        layout = QVBoxLayout(self)
        view = QTextBrowser()
        view.setObjectName("document")
        view.setMarkdown(markdown)
        layout.addWidget(view)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Copy", lambda: QApplication.clipboard().setText(self.markdown)))
        row.addWidget(button("Save as…", self.save))
        row.addWidget(button("Close", self.accept, "primary"))
        layout.addLayout(row)

    def save(self):
        target, _ = QFileDialog.getSaveFileName(self, "Save", self.filename, "Markdown (*.md)")
        if target:
            with open(target, "w", encoding="utf-8") as out:
                out.write(self.markdown)


class ActionsPage(QWidget):
    """Every decision, action, risk and question across meetings, with one-click close."""
    changed = Signal()

    def __init__(self, engine_factory):
        super().__init__()
        self.engine_factory = engine_factory
        self.loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 18)
        layout.setSpacing(12)
        layout.addWidget(label("Actions and decisions", "heading"))
        layout.addWidget(label("Everything agreed across your meetings. Tick an item when it is done.", "muted"))
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search items, owners or meetings")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.kind = QComboBox()
        for text, value in KINDS:
            self.kind.addItem(text, value)
        self.kind.currentIndexChanged.connect(self.refresh)
        self.closed = QCheckBox("Show closed")
        self.closed.toggled.connect(self.refresh)
        self.agenda = button("Draft next agenda", self.make_agenda, "primary",
                             tip="Build an agenda from open actions, questions and risks")
        row.addWidget(self.search, 1)
        row.addWidget(self.kind)
        row.addWidget(self.closed)
        row.addWidget(self.agenda)
        layout.addLayout(row)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Done", "Item", "Owner", "Due", "Type", "Meeting"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setWordWrap(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in [(0, 60), (2, 110), (3, 110), (4, 100), (5, 210)]:
            self.table.setColumnWidth(column, width)
        self.table.itemChanged.connect(self.toggled)
        layout.addWidget(self.table, 1)
        self.summary = label("", "muted")
        layout.addWidget(self.summary)

    def rows(self):
        store = self.engine_factory().store
        query = self.search.text().strip().lower()
        found = []
        for item in store.items(kind=self.kind.currentData(), limit=1000):
            if not self.closed.isChecked() and item["status"] in {"closed", "superseded"}:
                continue
            haystack = " ".join([item["text"], item["owner"], item["meeting_title"], item["discipline"]]).lower()
            if query and query not in haystack:
                continue
            found.append(item)
        return found

    def refresh(self, *_):
        rows = self.rows()
        self.loading = True
        self.table.setRowCount(len(rows))
        late = 0
        for index, item in enumerate(rows):
            done = QTableWidgetItem()
            done.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            done.setCheckState(Qt.CheckState.Checked if item["status"] == "closed" else Qt.CheckState.Unchecked)
            done.setData(Qt.ItemDataRole.UserRole, item["id"])
            self.table.setItem(index, 0, done)
            is_late = overdue(item)
            late += is_late
            due = item["due"] + (" · overdue" if is_late else "")
            cells = [item["text"], item["owner"] or "Unassigned", due or "No date", KIND_LABEL[item["kind"]],
                     f"{item['meeting_date']} · {item['meeting_title']}"]
            for column, text in enumerate(cells, 1):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                if column == 3 and is_late:
                    cell.setForeground(self.palette().link())
                self.table.setItem(index, column, cell)
        self.table.resizeRowsToContents()
        self.loading = False
        self.summary.setText(f"{len(rows)} items" + (f" · {late} overdue" if late else "") if rows else
                             "Nothing here yet. Items appear after a meeting has minutes.")

    def toggled(self, cell):
        if self.loading or cell.column() != 0:
            return
        status = "closed" if cell.checkState() == Qt.CheckState.Checked else "open"
        self.engine_factory().store.update_item(cell.data(Qt.ItemDataRole.UserRole), status=status)
        self.changed.emit()
        self.refresh()

    def make_agenda(self):
        items = self.engine_factory().store.items(limit=1000)
        TextDialog(self, "Draft agenda", draft_agenda(items), "agenda.md").exec()


class SpeakersDialog(QDialog):
    """Tell Coord who S1, S2… are, using the people in aliases.toml."""
    def __init__(self, parent, engine, meeting_id):
        super().__init__(parent)
        self.setWindowTitle("Who spoke?")
        self.resize(640, 420)
        self.engine, self.meeting_id = engine, meeting_id
        meeting = engine.store.meeting(meeting_id)
        samples = {}
        for segment in engine.store.segments(meeting_id):
            if segment["speaker"]:
                samples.setdefault(segment["speaker"], segment["text"])
        people = [c for c in engine.redactor().public_codes() if c["kind"] == "person"]
        layout = QVBoxLayout(self)
        layout.addWidget(label("Match each detected voice to a person. Minutes then use that person as owner and speaker.", "muted", wrap=True))
        form = QFormLayout()
        self.choices = {}
        for speaker, sample in samples.items():
            combo = QComboBox()
            combo.addItem("Not assigned", "")
            for person in people:
                combo.addItem(f"{person['code']} · {person['role'] or 'person'}", person["code"])
            combo.setCurrentIndex(max(0, combo.findData(meeting["speakers"].get(speaker, ""))))
            self.choices[speaker] = combo
            quote = label(f"“{sample[:110]}”", "muted", wrap=True)
            form.addRow(f"Voice {speaker}", combo)
            form.addRow("", quote)
        layout.addLayout(form)
        if not samples:
            layout.addWidget(label("No separate voices were detected for this meeting. Speaker detection needs the optional "
                                   "speaker model (see Settings › AI engine).", wrap=True))
        elif not people:
            layout.addWidget(label("Add people to aliases.toml (Open data folder) to assign voices.", wrap=True))
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        mapping = {speaker: combo.currentData() for speaker, combo in self.choices.items() if combo.currentData()}
        if mapping:
            self.engine.set_speakers(self.meeting_id, mapping)
        super().accept()


class Preferences(QDialog):
    """Settings grouped by task. Secrets never live here; they stay in environment variables."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(720, min(680, parent.screen().availableGeometry().height() - 80))
        self.preferences = parent.preferences
        self.controls = {}
        self.pages = {}
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        form = self.page("Audio")
        self.check(form, "microphone", "Include my microphone")
        for key, text, default in [("speaker_device", "Meeting audio output", "Windows default output"),
                                   ("microphone_device", "Microphone input", "Windows default microphone")]:
            control = QComboBox()
            control.setAccessibleName(text)
            control.setMinimumContentsLength(20)
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            control.addItem(default, "")
            saved = getattr(self.preferences, key)
            if saved:
                control.addItem(saved, saved)
                control.setCurrentIndex(1)
            self.add(form, key, text, control)
        self.device_note = self.note(form, "Computer sound is always captured. Select the speaker or headset your meeting uses; it may differ from the Windows default output.")
        self.refresh_audio = button("Refresh audio devices", self.refresh_devices)
        form.addRow(self.refresh_audio)

        form = self.page("Live captions")
        self.combo(form, "translation", "Live translation", ["none", "Arabic", "English"])
        self.spin(form, "caption_step_seconds", "Caption update interval (seconds)", 1, 10)
        self.text(form, "live_model", "Fast caption speech model", "blank = same as final, e.g. large-v3-turbo")
        self.text(form, "caption_model", "Translation model", "blank = minutes model, e.g. qwen3:4b")
        self.note(form, "A fast caption model keeps live text close to real time. The full recording is then "
                        "transcribed again with the accurate model before minutes are written.")

        form = self.page("Meetings")
        self.check(form, "detect_meetings", "Notice when a meeting starts on this PC and ask to record")
        self.check(form, "auto_start", "Record detected meetings without asking")
        self.spin(form, "auto_stop_seconds", "Stop after the meeting ends (seconds)", 30, 3600)
        self.check(form, "calendar", "Show upcoming meetings from Outlook on this PC")
        self.spin(form, "reminder_minutes", "Remind me before a meeting (minutes)", 0, 30)
        self.check(form, "auto_join_guest", "Send the Coord guest to calendar meetings automatically")
        self.text(form, "guest_name", "Guest display name")
        self.check(form, "close_to_tray", "Keep running in the notification area when the window is closed")
        self.check(form, "start_with_windows", "Start Coord when I sign in to Windows (minimised to the tray)")
        self.check(form, "action_reminders", "Remind me each morning about actions due today or overdue")
        self.note(form, "Recording is never hidden: the guest joins under a visible name and Coord shows a recording "
                        "indicator. Tell participants and follow your meeting policy.")

        form = self.page("AI engine")
        self.text(form, "llm_host", "Minutes AI server", "blank = this PC, e.g. http://10.0.0.20:11434")
        self.text(form, "speech_server", "Speech server", "blank = this PC, e.g. http://10.0.0.20:8000")
        self.text(form, "speech_server_model", "Speech server model", "e.g. Systran/faster-whisper-large-v3")
        self.note(form, "Leave blank to run everything on this PC. A company server must be on the private network; "
                        "public addresses are refused. Set COORDY_SERVER_KEY if the speech server needs a key. "
                        "Model names, language and privacy switches are in config.toml (Open data folder).")

        form = self.page("Sharing")
        self.combo(form, "delivery", "Delivery channel", ["teams", "email"])
        self.check(form, "auto_send", "Send completed minutes automatically")
        self.text(form, "smtp_host", "SMTP host")
        self.spin(form, "smtp_port", "SMTP STARTTLS port", 1, 65535)
        self.text(form, "smtp_user", "SMTP username")
        self.text(form, "sender", "Sender email")
        self.text(form, "recipients", "Recipients (comma separated)")
        self.note(form, "Credentials come from COORDY_TEAMS_WEBHOOK or COORDY_SMTP_PASSWORD in your Windows user "
                        "environment. Only redacted minutes are sent.")

        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        actions.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        layout.addWidget(actions)
        if sys.platform == "win32":
            self.refresh_devices()
        else:
            self.refresh_audio.setEnabled(False)

    # builders -----------------------------------------------------------
    def page(self, title):
        body = QWidget()
        body.setObjectName("settingsBody")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(18, 18, 18, 18)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(12)
        outer.addLayout(form)
        outer.addStretch()
        form.setProperty("tab", self.tabs.addTab(body, title))
        return form

    def add(self, form, key, text, control):
        self.controls[key] = control
        self.pages[key] = form.property("tab")
        if text:
            form.addRow(text, control)
        else:
            form.addRow(control)

    def check(self, form, key, text):
        control = QCheckBox(text)
        control.setChecked(getattr(self.preferences, key))
        self.add(form, key, "", control)

    def combo(self, form, key, text, options):
        control = QComboBox()
        control.addItems(options)
        control.setCurrentText(getattr(self.preferences, key))
        self.add(form, key, text, control)

    def spin(self, form, key, text, low, high):
        control = QSpinBox()
        control.setRange(low, high)
        control.setValue(getattr(self.preferences, key))
        self.add(form, key, text, control)

    def text(self, form, key, text, placeholder=""):
        control = QLineEdit(getattr(self.preferences, key))
        control.setPlaceholderText(placeholder)
        self.add(form, key, text, control)

    def note(self, form, text):
        note = label(text, "muted", wrap=True)
        form.addRow(note)
        return note

    def select(self, key):
        """Show the tab holding a setting."""
        self.tabs.setCurrentIndex(self.pages[key])

    # audio devices ------------------------------------------------------
    def refresh_devices(self):
        self.refresh_audio.setEnabled(False)
        self.device_note.setText("Checking Windows audio outputs and microphones…")
        self.parent().spawn(lambda emit: list_audio_devices(), self.populate_devices, self.device_error)

    def populate_devices(self, catalog):
        for key, kind, default in [("speaker_device", "speakers", "Windows default output"),
                                   ("microphone_device", "microphones", "Windows default microphone")]:
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
        values = {}
        for key, control in self.controls.items():
            values[key] = (control.currentData() if key in {"speaker_device", "microphone_device"}
                           else control.isChecked() if isinstance(control, QCheckBox)
                           else control.value() if isinstance(control, QSpinBox)
                           else control.currentText() if isinstance(control, QComboBox)
                           else control.text().strip())
        if values["llm_host"] != self.preferences.llm_host or values["speech_server"] != self.preferences.speech_server:
            from ..config import load_settings
            from dataclasses import replace
            try:
                apply_server(load_settings(), replace(self.preferences, **values))
            except ValueError as exc:
                self.select("llm_host")
                QMessageBox.warning(self, "AI server", str(exc))
                return
        for key, value in values.items():
            setattr(self.preferences, key, value)
        super().accept()
