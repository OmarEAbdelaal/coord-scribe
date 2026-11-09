"""Bounded live transcription, followed by the existing minutes pipeline."""
import json
import queue
import wave
import logging
import threading
from datetime import datetime
from dataclasses import replace
import uuid

from ..pipeline import Engine
from ..llm import Ollama
from ..transcribe import Transcriber
from .capture import WindowsCapture

log = logging.getLogger(__name__)


class CaptureSession:
    def __init__(self, settings, preferences, emit, *, capture=None, transcriber=None, engine=None):
        self.settings = settings
        self.preferences = preferences
        self.emit = emit
        self.capture = capture or WindowsCapture(
            preferences.microphone, preferences.speaker_device, preferences.microphone_device,
            on_levels=lambda levels: self.emit("audio_levels", levels),
            on_devices=lambda devices: self.emit("audio_devices", devices),
            chunk_seconds=preferences.caption_chunk_seconds)
        self.transcriber = transcriber or Transcriber(replace(settings.whisper, beam_size=1), settings.glossary(),
            status=lambda message: self.emit("speech_state", {"message": message, "busy": True}))
        self.engine = engine or Engine(settings, transcriber=self.transcriber)
        self._translation = preferences.translation
        self._translation_lock = threading.Lock()
        self.chunks = queue.Queue(maxsize=2)
        self.translation_queue = queue.Queue(maxsize=1)
        self._owns_caption_llm = engine is None
        self.caption_llm = engine.llm if engine is not None else Ollama(
            replace(settings.llm, model=preferences.caption_model or settings.llm.model,
                    num_ctx=2048, timeout_seconds=20, think=False), settings.privacy)
        self.segments = []
        self.translations = []
        self.failure = None
        self.incomplete = False

    def set_translation(self, language):
        if language not in {"none", "Arabic", "English"}:
            raise ValueError("Choose none, Arabic or English")
        with self._translation_lock:
            self._translation = language

    def stop(self, reason="user request"):
        log.info("Capture stop requested: %s", reason)
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
                if len(audio) < 8000:
                    self.emit("speech_state", {"message": "Audio fragment too short for captions", "busy": False})
                    continue
                self.emit("speech_state", {"message": f"Transcribing captured audio at {offset:.0f}s", "busy": True})
                segments = self.transcriber.transcribe(audio)
                self.emit("speech_state", {"message": "Speech recognized" if segments else
                    "Audio processed, but no speech recognized in this chunk. Check the source levels and recording.", "busy": False})
                for segment in segments:
                    segment.start += offset
                    segment.end += offset
                    self.segments.append(segment)
                    text = self.engine.redactor().redact(segment.text)
                    self.emit("transcript", {**segment.to_dict(), "text": text})
                    with self._translation_lock:
                        target = self._translation
                    if target in {"Arabic", "English"}:
                        caption = {"start": segment.start, "end": segment.end, "language": target, "source": text}
                        self._offer_latest(self.translation_queue, caption)

            except Exception as exc:
                self.failure = exc
                self.capture.stop()
            finally:
                self.chunks.task_done()

    @staticmethod
    def _offer_latest(pending, item):
        dropped = False
        while True:
            try:
                pending.put_nowait(item)
                return dropped
            except queue.Full:
                try:
                    pending.get_nowait()
                    pending.task_done()
                    dropped = True
                except queue.Empty:
                    pass

    def _translate(self):
        try:
            while True:
                caption = self.translation_queue.get()
                try:
                    if caption is None:
                        return
                    with self._translation_lock:
                        target = self._translation
                    if target != caption["language"]:
                        continue
                    self.emit("translation_state", "Translating latest caption…")
                    translated = self.caption_llm.chat_json(
                        f"Translate the text into {target}. Preserve codes such as P01. Return JSON with text.",
                        caption["source"], {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                        max_tokens=256)
                    text = self.engine.redactor().guard(translated).get("text", "")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Empty caption translation")
                    result = {key: value for key, value in caption.items() if key != "source"}
                    result["text"] = text
                    self.translations.append(result)
                    self.emit("translation", result)
                except Exception:
                    self.emit("caption_translation_error", caption)
                finally:
                    self.translation_queue.task_done()
        finally:
            if self._owns_caption_llm:
                self.caption_llm.close()

    def run(self):
        title = datetime.now().strftime("Meeting %Y-%m-%d %H-%M-%S")
        path = self.settings.paths.inbox / f"{title}-{uuid.uuid4().hex[:6]}.wav"
        consumer = threading.Thread(target=self._consume, name="coord-live-transcription", daemon=True)
        translator = threading.Thread(target=self._translate, name="coord-caption-translation", daemon=True)
        translator.start()
        consumer.start()
        def chunk(audio, offset):
            if self._offer_latest(self.chunks, (audio, offset)):
                self.incomplete = True
                self.emit("status", "Live captions are behind; catching up to recent audio. Full recording will be transcribed after stop.")
        try:
            self.capture.record(path, chunk, lambda: self.emit("status", "Recording speaker audio" + (" and microphone" if self.preferences.microphone else "")))
        finally:
            self.emit("status", "Finishing transcript…")
            self.chunks.put(None)
            consumer.join()
            self.translation_queue.put(None)
            translator.join()
        with wave.open(str(path), "rb") as recorded:
            duration = recorded.getnframes() / recorded.getframerate()
        if duration < 1:
            raise RuntimeError(f"Capture saved only {duration:.2f} seconds. Recording stopped too early; check audio devices and logs/desktop.log. File retained: {path}")
        if self.failure:
            raise RuntimeError(f"Live processing failed; recording retained at {path}. {self.failure}")
        self.emit("speech_state", {"message": "Transcription complete; generating minutes", "busy": False})
        self.emit("status", "Generating local minutes…")
        if isinstance(self.transcriber, Transcriber):
            self.transcriber.settings = self.settings.whisper
        segments = None if self.incomplete else self.segments
        mid = self.engine.process(path, title=title, segments=segments,
                                  stage=lambda name, frac=None: self.emit("status", name))
        if self.translations:
            (self.settings.paths.redacted / f"{mid}.translation.json").write_text(
                json.dumps(self.translations, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("meeting", mid)
        return mid
