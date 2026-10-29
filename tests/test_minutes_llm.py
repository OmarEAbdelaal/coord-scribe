import httpx
import pytest

from coordy.config import LLMSettings, PrivacySettings
from coordy.diarize import Turn, assign_speakers, normalise_labels
from coordy.llm import LLMError, Ollama, RemoteLLMBlocked
from coordy.minutes import chunk_lines, extract_minutes, fmt_ts
from coordy.transcribe import Segment
from tests.conftest import MINUTES, FakeOllama


def llm_with(handler):
    return Ollama(LLMSettings(), PrivacySettings(), transport=httpx.MockTransport(handler))


def test_fmt_ts():
    assert fmt_ts(3725.9) == "01:02:05"


def test_chunk_lines_overlap():
    lines = [f"line {i:02d} " + "x" * 40 for i in range(20)]
    chunks = chunk_lines(lines, 200, overlap=1)
    assert len(chunks) > 1
    assert all(len(c) <= 260 for c in chunks)
    assert chunks[1].splitlines()[0] == chunks[0].splitlines()[-1]
    assert chunk_lines([], 100) == []


def test_remote_llm_blocked():
    with pytest.raises(RemoteLLMBlocked):
        Ollama(LLMSettings(host="https://api.example.com"), PrivacySettings())
    Ollama(LLMSettings(host="http://192.168.1.5:11434"), PrivacySettings(allow_remote_llm=True))


def test_structured_request_and_think_fallback():
    calls = []

    def handler(request):
        import json

        body = json.loads(request.content)
        calls.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(200, json={"message": {"content": json.dumps(MINUTES)}})

    minutes = extract_minutes(llm_with(handler), "system", ["[00:00:01] P01: hello"], 10000)
    assert minutes.decisions[0].owner == "P01"
    assert calls[0]["format"]["type"] == "object"
    assert calls[0]["options"]["num_ctx"] == 32768
    assert "think" not in calls[1]


def test_missing_model_message():
    with pytest.raises(LLMError, match="ollama pull"):
        extract_minutes(llm_with(lambda r: httpx.Response(404, json={"error": "not found"})), "s", ["x"], 100)


def test_long_meeting_is_chunked_then_consolidated():
    fake = FakeOllama()
    lines = [f"[00:00:{i:02d}] P01: " + "word " * 30 for i in range(40)]
    extract_minutes(llm_with(fake), "system", lines, 1500)
    assert len(fake.requests) > 2
    assert "partial minutes" in fake.requests[-1]["messages"][0]["content"]


def test_assign_speakers():
    turns = normalise_labels([Turn(0, 5, "SPEAKER_03"), Turn(5, 10, "SPEAKER_00")])
    assert [t.speaker for t in turns] == ["S1", "S2"]
    segs = assign_speakers([Segment(0.5, 4.0, "a"), Segment(4.0, 9.0, "b"), Segment(10.5, 11.0, "c"), Segment(30, 31, "d")], turns)
    assert [s.speaker for s in segs] == ["S1", "S2", "S2", ""]
