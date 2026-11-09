"""Bounded live transcription, followed by the existing minutes pipeline."""
import json
import queue
import threading
from datetime import datetime
import uuid

from ..pipeline import Engine
from ..transcribe import Transcriber
from .capture import WindowsCapture


class CaptureSession:
    def __init__(self, settings, preferences, emit, *, capture=None, transcriber=None, engine=None):
        self.settings = settings
        self.preferences = preferences
        self.emit = emit
        self.capture = capture or WindowsCapture(preferences.microphone)
        self.transcriber = transcriber or Transcriber(settings.whisper, settings.glossary())
        self.engine = engine or Engine(settings, transcriber=self.transcriber)
        self.chunks = queue.Queue(maxsize=6)
        self.segments = []
        self.translations = []
        self.failure = None
        self.incomplete = False

    def stop(self):
        self.capture.stop()

    def _consume(self):
        while True:
            item = self.chunks.get()
            try:
                if item is None:
                    return
                if self.failure:
                    continue
                audio, offset = item
                for segment in self.transcriber.transcribe(audio):
                    segment.start += offset
                    segment.end += offset
                    self.segments.append(segment)
                    text = self.engine.redactor().redact(segment.text)
                    self.emit("transcript", {**segment.to_dict(), "text": text})
                    if self.preferences.translation in {"Arabic", "English"}:
                        translated = self.engine.llm.chat_json(
                            f"Translate the text into {self.preferences.translation}. Preserve codes such as P01. Return JSON with text.",
                            text, {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
                        text = self.engine.redactor().guard(translated).get("text", "")
                        self.translations.append({"start": segment.start, "end": segment.end, "text": text})
                        self.emit("translation", text)
            except Exception as exc:
                self.failure = exc
                self.capture.stop()
            finally:
                self.chunks.task_done()

    def run(self):
        title = datetime.now().strftime("Meeting %Y-%m-%d %H-%M-%S")
        path = self.settings.paths.inbox / f"{title}-{uuid.uuid4().hex[:6]}.wav"
        consumer = threading.Thread(target=self._consume, name="coord-live-transcription", daemon=True)
        consumer.start()
        def chunk(audio, offset):
            try:
                self.chunks.put_nowait((audio, offset))
            except queue.Full:
                self.incomplete = True
                self.emit("status", "Live transcription is behind. Full recording will be transcribed after stop.")
        try:
            self.capture.record(path, chunk, lambda: self.emit("status", "Recording speaker audio" + (" and microphone" if self.preferences.microphone else "")))
        finally:
            self.emit("status", "Finishing transcript…")
            self.chunks.put(None)
            consumer.join()
        if self.failure:
            raise RuntimeError(f"Live processing failed; recording retained at {path}. {self.failure}")
        self.emit("status", "Generating local minutes…")
        segments = None if self.incomplete else self.segments
        mid = self.engine.process(path, title=title, segments=segments,
                                  stage=lambda name, frac=None: self.emit("status", name))
        if self.translations:
            (self.settings.paths.redacted / f"{mid}.translation.json").write_text(
                json.dumps(self.translations, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("meeting", mid)
        return mid
