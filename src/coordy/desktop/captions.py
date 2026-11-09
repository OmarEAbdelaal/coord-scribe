"""Readable live captions with an inline local-translation selector."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout, QWidget


class CaptionPanel(QWidget):
    translation_changed = Signal(str)

    def __init__(self, target="none", parent=None, chunk_seconds=4):
        super().__init__(parent)
        self.segment = None
        self.translation_time = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        controls = QHBoxLayout()
        title = QLabel("Live captions")
        title.setObjectName("captionHeading")
        controls.addWidget(title)
        controls.addStretch()
        controls.addWidget(QLabel("Translate to"))
        self.target = QComboBox()
        self.target.setAccessibleName("Caption translation language")
        for label, value in [("Off · original speech", "none"), ("Arabic", "Arabic"), ("English", "English")]:
            self.target.addItem(label, value)
        self.target.setCurrentIndex(max(0, self.target.findData(target)))
        controls.addWidget(self.target)
        layout.addLayout(controls)
        self.notice = QLabel(f"Audio window: {chunk_seconds}s plus model processing. Translation runs separately.")
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.timestamp = QLabel("Waiting for speech")
        self.timestamp.setObjectName("muted")
        self.timestamp.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.timestamp)
        layout.addWidget(QLabel("Original speech"))
        self.original = QTextBrowser()
        self.original.setObjectName("captionText")
        self.original.setMinimumHeight(90)
        self.original.setAccessibleName("Live original caption")
        self.original.setPlaceholderText("Start capture to see Arabic and English speech here.")
        layout.addWidget(self.original, 1)
        self.translated_label = QLabel("Translation")
        layout.addWidget(self.translated_label)
        self.translation_status = QLabel("")
        self.translation_status.setObjectName("muted")
        layout.addWidget(self.translation_status)
        self.translated = QTextBrowser()
        self.translated.setObjectName("captionText")
        self.translated.setMinimumHeight(90)
        self.translated.setAccessibleName("Live translated caption")
        layout.addWidget(self.translated, 1)
        self.target.currentIndexChanged.connect(self._target_changed)
        self._update_translation_view()

    @property
    def language(self):
        return self.target.currentData()

    def _update_translation_view(self):
        enabled = self.language != "none"
        self.translated.setVisible(enabled)
        self.translated_label.setVisible(enabled)
        self.translation_status.setVisible(enabled)
        self.translation_status.clear()
        self.translation_time = -1
        self.translated_label.setText(f"Translation · {self.language}")
        self.translated.clear()
        self.translated.setPlaceholderText("Waiting for the next translated speech segment…")

    def _target_changed(self, index):
        self._update_translation_view()
        self.translation_changed.emit(self.language)

    def reset(self):
        self.segment = None
        self.translation_time = -1
        self.translation_status.clear()
        self.original.clear()
        self.translated.clear()
        self.timestamp.setText("Waiting for speech")

    def show_segment(self, segment):
        self.segment = (segment["start"], segment["end"])
        self.original.setPlainText(segment["text"])
        seconds = int(segment["start"])
        language = segment.get("language") or "auto"
        self.timestamp.setText(f"{seconds // 60:02}:{seconds % 60:02} · {language}")

    def show_translation(self, segment):
        # Keep the newest completed translation visible with its own timestamp.
        if self.language == segment.get("language") and segment["start"] >= self.translation_time:
            self.translation_time = segment["start"]
            seconds = int(segment["start"])
            self.translation_status.setText(f"Translated audio at {seconds // 60:02}:{seconds % 60:02}")
            self.translated.setPlainText(segment["text"])

    def show_translation_error(self, segment):
        if self.language == segment.get("language") and segment["start"] >= self.translation_time:
            self.translation_status.setText("Translation failed")
            self.translated.setPlainText("Translation unavailable. Check local Ollama; recording and original captions continue.")
