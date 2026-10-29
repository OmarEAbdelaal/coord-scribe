"""Speech to text with faster-whisper, fully on this computer."""

from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import asdict, dataclass

from .config import WhisperSettings
from .gpu import cuda_device_count, prepare_cuda

log = logging.getLogger(__name__)
SAMPLE_RATE = 16000


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str = ""
    language: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def load_audio(path: str):
    """Decode any audio/video file to 16 kHz mono float32 (bundled FFmpeg via PyAV)."""
    from faster_whisper import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


class Transcriber:
    def __init__(self, settings: WhisperSettings, glossary: str = ""):
        self.settings = settings
        self.glossary = glossary
        self._model = None
        self._device = ""
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        return self._device

    def _resolve(self) -> tuple[str, str]:
        device = self.settings.device
        if device == "auto":
            device = "cuda" if cuda_device_count() > 0 else "cpu"
        compute = self.settings.compute_type
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def _load(self, device: str, compute: str):
        from faster_whisper import WhisperModel

        prepare_cuda()
        log.info("Loading Whisper %s on %s (%s)", self.settings.model, device, compute)
        self._model = WhisperModel(self.settings.model, device=device, compute_type=compute)
        self._device = device

    def model(self):
        if self._model is None:
            device, compute = self._resolve()
            try:
                self._load(device, compute)
            except (RuntimeError, ValueError) as exc:
                if device != "cuda" or self.settings.device == "cuda":
                    raise
                log.warning("Could not load Whisper on GPU (%s); using CPU", exc)
                self._load("cpu", "int8")
        return self._model

    def _options(self) -> dict:
        s = self.settings
        options = {
            "language": s.language or None,
            "multilingual": s.multilingual,
            "beam_size": s.beam_size,
            "vad_filter": s.vad,
            # Stops Whisper repeating itself across long silences.
            "condition_on_previous_text": False,
            # Whisper only reads ~224 tokens of prompt; the glossary goes first.
            "initial_prompt": self.glossary[:800] or None,
        }
        supported = inspect.signature(self.model().transcribe).parameters
        return {k: v for k, v in options.items() if k in supported}

    def _run(self, audio, progress) -> list[Segment]:
        segments, info = self.model().transcribe(audio, **self._options())
        duration = float(getattr(info, "duration", 0) or 0)
        out: list[Segment] = []
        for s in segments:
            text = s.text.strip()
            if text:
                out.append(Segment(float(s.start), float(s.end), text, language=getattr(s, "language", "") or info.language))
            if progress and duration:
                progress(min(1.0, s.end / duration))
        return out

    def transcribe(self, audio, progress=None) -> list[Segment]:
        with self._lock:
            try:
                return self._run(audio, progress)
            except RuntimeError as exc:
                if self._device != "cuda" or self.settings.device == "cuda":
                    raise
                # Typical: cublas64_12.dll / cudnn missing or unsupported GPU.
                log.warning("GPU transcription failed (%s); retrying on CPU", exc)
                self._load("cpu", "int8")
                return self._run(audio, progress)
