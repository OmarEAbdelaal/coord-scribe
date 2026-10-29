"""Who spoke when (pyannote, runs locally) and matching speakers to transcript lines."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .config import DiarizationSettings
from .transcribe import SAMPLE_RATE, Segment

log = logging.getLogger(__name__)


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


class DiarizationUnavailable(RuntimeError):
    pass


class Diarizer:
    def __init__(self, settings: DiarizationSettings, hf_token: str = ""):
        self.settings = settings
        self.hf_token = hf_token
        self._pipeline = None
        self._lock = threading.Lock()

    def pipeline(self):
        if self._pipeline is None:
            try:
                import torch
                from pyannote.audio import Pipeline
            except ImportError as exc:
                raise DiarizationUnavailable(
                    "pyannote.audio is not installed (install with the 'diarize' extra)"
                ) from exc
            try:
                pipeline = Pipeline.from_pretrained(self.settings.model, token=self.hf_token or None)
            except TypeError:  # pyannote 3.x
                pipeline = Pipeline.from_pretrained(self.settings.model, use_auth_token=self.hf_token or None)
            if pipeline is None:
                raise DiarizationUnavailable(
                    f"Could not load {self.settings.model}. Accept its terms on huggingface.co and set hf_token."
                )
            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
            self._pipeline = pipeline
        return self._pipeline

    def diarize(self, audio) -> list[Turn]:
        import torch

        with self._lock:
            pipeline = self.pipeline()
            kwargs = {}
            if self.settings.min_speakers:
                kwargs["min_speakers"] = self.settings.min_speakers
            if self.settings.max_speakers:
                kwargs["max_speakers"] = self.settings.max_speakers
            # Passing the decoded waveform avoids pyannote's own file decoding (torchcodec/FFmpeg).
            waveform = torch.from_numpy(audio).unsqueeze(0)
            output = pipeline({"waveform": waveform, "sample_rate": SAMPLE_RATE}, **kwargs)
        # pyannote 4 returns DiarizeOutput; 3.x returns an Annotation.
        annotation = getattr(output, "exclusive_speaker_diarization", None) or getattr(
            output, "speaker_diarization", output
        )
        return normalise_labels(
            [Turn(float(t.start), float(t.end), str(label)) for t, _, label in annotation.itertracks(yield_label=True)]
        )


def normalise_labels(turns: list[Turn]) -> list[Turn]:
    """SPEAKER_00, SPEAKER_01... -> S1, S2... in order of first appearance."""
    mapping: dict[str, str] = {}
    for turn in sorted(turns, key=lambda t: t.start):
        mapping.setdefault(turn.speaker, f"S{len(mapping) + 1}")
    return [Turn(t.start, t.end, mapping[t.speaker]) for t in turns]


def assign_speakers(segments: list[Segment], turns: list[Turn], max_gap: float = 1.0) -> list[Segment]:
    """Give each transcript segment the speaker it overlaps most (or the nearest one)."""
    if not turns:
        return segments
    for seg in segments:
        overlap: dict[str, float] = {}
        for turn in turns:
            o = min(seg.end, turn.end) - max(seg.start, turn.start)
            if o > 0:
                overlap[turn.speaker] = overlap.get(turn.speaker, 0.0) + o
        if overlap:
            seg.speaker = max(overlap, key=overlap.get)
            continue
        mid = (seg.start + seg.end) / 2
        nearest = min(turns, key=lambda t: min(abs(mid - t.start), abs(mid - t.end)))
        if min(abs(mid - nearest.start), abs(mid - nearest.end)) <= max_gap:
            seg.speaker = nearest.speaker
    return segments
