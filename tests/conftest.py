import json
import math
import struct
import wave
from pathlib import Path

import httpx
import pytest

from coordy.config import load_settings
from coordy.diarize import Turn
from coordy.llm import Ollama
from coordy.pipeline import Engine
from coordy.transcribe import Segment

ALIASES = """
[[person]]
name = "Omar Elsayed"
code = "P01"
role = "Lead Architect"
variants = ["Omar", "عمر"]

[[person]]
name = "Ahmed Hassan"
code = "P02"
role = "MEP Coordinator"
variants = ["Ahmed", "أحمد"]

[[org]]
name = "Nile Developments"
code = "ORG01"
role = "Client"

[[project]]
name = "Marina Tower"
code = "PRJ01"
role = "Mixed-use tower"
"""

SEGMENTS = [
    Segment(0.0, 4.0, "Good morning, this is Omar Elsayed for Nile Developments.", language="en"),
    Segment(4.5, 9.0, "Ahmed, the MEP shafts on level 12 clash with the core wall.", language="en"),
    Segment(9.5, 14.0, "تمام يا عمر، هنعدل الشافت ونبعت الموديل يوم الخميس", language="ar"),
    Segment(14.5, 18.0, "Decision: we freeze the Marina Tower facade module at 1.5 m. Call me on +971 50 123 4567.", language="en"),
]
TURNS = [Turn(0.0, 4.2, "SPEAKER_01"), Turn(4.3, 9.2, "SPEAKER_01"), Turn(9.3, 14.2, "SPEAKER_00"), Turn(14.3, 18.0, "SPEAKER_01")]


class FakeTranscriber:
    device = "cpu"

    def __init__(self, segments=None):
        self.segments = segments or SEGMENTS

    def transcribe(self, audio, progress=None):
        if progress:
            progress(1.0)
        return [Segment(s.start, s.end, s.text, language=s.language) for s in self.segments]


class FakeDiarizer:
    def diarize(self, audio):
        from coordy.diarize import normalise_labels

        return normalise_labels(list(TURNS))


MINUTES = {
    "summary": "P01 and P02 coordinated MEP shafts on level 12 for PRJ01.",
    "topics": ["MEP shafts", "Facade module"],
    "decisions": [{"text": "Freeze PRJ01 facade module at 1.5 m", "owner": "P01", "due": "", "discipline": "Facade", "timestamp": "00:00:14"}],
    "actions": [{"text": "Revise shaft on level 12 and issue model", "owner": "P02", "due": "Thursday", "discipline": "MEP", "timestamp": "00:00:09"}],
    "risks": [{"text": "Core wall clash may delay structure", "owner": "", "due": "", "discipline": "Structure", "timestamp": "00:00:04"}],
    "open_questions": [],
}


class FakeOllama:
    """httpx transport that imitates Ollama and records what it was sent."""

    def __init__(self, reply=None):
        self.reply = reply or MINUTES
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.0-test"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:14b"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.reply(body) if callable(self.reply) else self.reply
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(reply)}})


def write_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return path


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDY_HOME", str(tmp_path / "Coordy"))
    s = load_settings()
    s.paths.aliases.write_text(ALIASES, encoding="utf-8")
    return s


@pytest.fixture
def fake_ollama():
    return FakeOllama()


@pytest.fixture
def engine(settings, fake_ollama):
    llm = Ollama(settings.llm, settings.privacy, transport=httpx.MockTransport(fake_ollama))
    return Engine(settings, transcriber=FakeTranscriber(), diarizer=FakeDiarizer(), llm=llm)


@pytest.fixture
def recording(settings):
    return write_wav(settings.paths.inbox / "2026-10-28 coordination meeting.wav")
