"""Bounded live transcription, followed by the existing minutes pipeline."""
import json
import queue
import wave
import logging
import threading
from datetime import datetime
from dataclasses import replace
import uuid

import numpy as np

from ..pipeline import Engine
from ..llm import Ollama
from ..transcribe import Transcriber, make_transcriber
from .capture import WindowsCapture

log = logging.getLogger(__name__)


RATE = 16000
MAX_UTTERANCE = 15 * RATE   # commit even without a pause, so a caption never grows unbounded
PAUSE = RATE // 2           # this much trailing quiet ends an utterance
QUIET = 0.008               # RMS below this is treated as a pause
CATCH_UP = 3                # pending steps beyond this are merged into one pass

TRANSLATE = ("Translate the text into {target}. It is a caption from a construction and design coordination "
             "meeting: use the standard AECO terms (tender = مناقصة, BOQ = جدول الكميات, programme = البرنامج الزمني, "
             "milestone = مرحلة رئيسية, shop drawings = مخططات تنفيذية). Preserve codes such as P01, numbers and "
             "abbreviations. Return JSON with text.")


class CaptureSession:
    def __init__(self, settings, preferences, emit, *, capture=None, transcriber=None, engine=None):
        self.settings = settings
        self.preferences = preferences
        self.emit = emit
        self.capture = capture or WindowsCapture(
            preferences.microphone, preferences.speaker_device, preferences.microphone_device,
            on_levels=lambda levels: self.emit("audio_levels", levels),
            on_devices=lambda devices: self.emit("audio_devices", devices),
            chunk_seconds=preferences.caption_step_seconds)
        # Live captions decode with a single beam and, optionally, a faster model;
        # the full recording is then transcribed again with the configured model.
        live = replace(settings.whisper, beam_size=1, model=preferences.live_model or settings.whisper.model)
        self.two_pass = transcriber is None and live.model != settings.whisper.model
        self.transcriber = transcriber or make_transcriber(live, settings.glossary(),
            status=lambda message: self.emit("speech_state", {"message": message, "busy": True}),
            allow_remote=settings.privacy.allow_remote_llm)
        self.engine = engine or (Engine(settings) if self.two_pass else Engine(settings, transcriber=self.transcriber))
        self._translation = preferences.translation
        self._translation_lock = threading.Lock()
        self.chunks = queue.Queue(maxsize=20)
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

    def _commit(self, segments, origin):
        for segment in segments:
            segment.start += origin
            segment.end += origin
            self.segments.append(segment)
            text = self.engine.redactor().redact(segment.text)
            self.emit("transcript", {**segment.to_dict(), "text": text})
            with self._translation_lock:
                target = self._translation
            if target in {"Arabic", "English"}:
                caption = {"start": segment.start, "end": segment.end, "language": target, "source": text}
                self._offer_latest(self.translation_queue, caption)

    def _step(self, buffer, origin, flush=False):
        """Transcribe the open utterance. Returns what is still open: (buffer, origin).

        The utterance grows one capture step at a time and is re-decoded each step, so
        words appear about a second after they are spoken ("partial") and are corrected
        as context arrives. A pause, or MAX_UTTERANCE, commits the text as final.
        """
        empty = np.zeros(0, np.float32)
        if len(buffer) < PAUSE:
            return (empty, origin) if flush else (buffer, origin)
        self.emit("speech_state", {"message": f"Transcribing audio at {origin:.0f}s", "busy": True})
        segments = self.transcriber.transcribe(buffer)
        paused = flush or float(np.sqrt(np.mean(np.square(buffer[-PAUSE:])))) < QUIET
        self.emit("speech_state", {"message": "Listening" if segments or not paused else
            "Listening · no speech in the last few seconds. Check the source levels if someone is talking.", "busy": False})
        if not segments:
            return (empty, origin) if paused or len(buffer) >= MAX_UTTERANCE else (buffer, origin)
        if paused or len(buffer) >= MAX_UTTERANCE and len(segments) == 1:
            self._commit(segments, origin)
            self.emit("partial", {"text": ""})
            return empty, origin
        if len(buffer) >= MAX_UTTERANCE:
            # Keep the unfinished last phrase open and commit everything before it.
            cut = min(len(buffer), max(0, int(segments[-1].start * RATE)))
            self._commit(segments[:-1], origin)
            segments = [segments[-1]]
            buffer, origin = buffer[cut:], origin + cut / RATE
            segments[0].start = 0.0
        self.emit("partial", {"start": origin + segments[0].start, "language": segments[-1].language,
                              "text": self.engine.redactor().redact(" ".join(s.text for s in segments))})
        return buffer, origin

    def _consume(self):
        buffer, origin = np.zeros(0, np.float32), 0.0
        while True:
            item = self.chunks.get()
            taken = 1
            try:
                if item is None:
                    if not self.failure:
                        self._step(buffer, origin, flush=True)
                    return
                if self.failure:
                    continue
                audio, offset = item
                if len(buffer) and abs(origin + len(buffer) / RATE - offset) > 0.05:
                    # Audio was skipped under load: close the utterance before the gap.
                    self._step(buffer, origin, flush=True)
                    buffer = buffer[:0]
                if not len(buffer):
                    origin = offset
                buffer = np.concatenate([buffer, audio])
                # Far behind: fold waiting steps into this pass instead of decoding each one.
                while self.chunks.qsize() > CATCH_UP:
                    extra = self.chunks.get()
                    taken += 1
                    if extra is None:
                        self.chunks.put(None)
                        break
                    if abs(origin + len(buffer) / RATE - extra[1]) > 0.05:
                        self._step(buffer, origin, flush=True)
                        buffer, origin = buffer[:0], extra[1]
                    buffer = np.concatenate([buffer, extra[0]])
                buffer, origin = self._step(buffer, origin)
            except Exception as exc:
                self.failure = exc
                self.capture.stop()
            finally:
                for _ in range(taken):
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
                        TRANSLATE.format(target=target),
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
        if isinstance(self.transcriber, Transcriber) and not self.two_pass:
            self.transcriber.settings = self.settings.whisper
        segments = None if self.incomplete or self.two_pass else self.segments
        mid = self.engine.process(path, title=title, segments=segments,
                                  stage=lambda name, frac=None: self.emit("status", name))
        if self.translations:
            (self.settings.paths.redacted / f"{mid}.translation.json").write_text(
                json.dumps(self.translations, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("meeting", mid)
        return mid
