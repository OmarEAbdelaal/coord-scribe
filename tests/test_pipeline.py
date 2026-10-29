import csv
import json

import pytest

from coordy.cli import main
from coordy.pipeline import JobQueue, guess_date, inbox_files

REAL_NAMES = ["Omar", "Elsayed", "Ahmed", "عمر", "Nile Developments", "Marina Tower", "123 4567"]


def test_process_end_to_end(engine, settings, recording, fake_ollama):
    mid = engine.process(recording, project="PRJ01")
    p = settings.paths
    assert mid == "2026-10-28_2026-10-28-coordination-meeting"

    # raw keeps the real words; everything else is redacted
    assert "Omar Elsayed" in (p.raw / f"{mid}.json").read_text(encoding="utf-8")
    redacted = (p.redacted / f"{mid}.md").read_text(encoding="utf-8")
    assert "P01" in redacted and "ORG01" in redacted and "PRJ01" in redacted and "[PHONE]" in redacted
    for name in REAL_NAMES:
        assert name not in redacted
        assert name not in json.dumps(engine.store.segments(mid), ensure_ascii=False)

    # the local model only ever saw redacted text
    sent = json.dumps(fake_ollama.requests, ensure_ascii=False)
    for name in REAL_NAMES:
        assert name not in sent
    assert "S1: Good morning" in sent and "S2: تمام" in sent

    meeting = engine.store.meeting(mid)
    assert meeting["status"] == "done" and meeting["language"] == "en,ar"
    assert {i["kind"] for i in engine.store.items(meeting_id=mid)} == {"decision", "action", "risk"}
    assert "Freeze PRJ01 facade" in (p.minutes / f"{mid}.md").read_text(encoding="utf-8")

    # recording moved out of the inbox
    assert not recording.exists()
    assert (p.audio / recording.name).exists()
    assert inbox_files(settings, engine.store) == []


def test_speakers_regenerate_and_named(engine, settings, recording):
    mid = engine.process(recording)
    with pytest.raises(ValueError):
        engine.set_speakers(mid, {"S1": "Omar"})
    engine.set_speakers(mid, {"S1": "P01", "S2": "P02"})
    assert "P01: Good morning" in (settings.paths.redacted / f"{mid}.md").read_text(encoding="utf-8")

    manual = engine.store.add_item(mid, {"kind": "action", "text": "Hand-written action"})
    engine.summarise(mid)
    texts = [i["text"] for i in engine.store.items(meeting_id=mid)]
    assert "Hand-written action" in texts and len(texts) == 4
    assert manual in [i["id"] for i in engine.store.items(meeting_id=mid)]

    named = engine.write_named_minutes(mid).read_text(encoding="utf-8")
    assert "Marina Tower" in named and "Ahmed Hassan" in named


def test_search_fts_and_items(engine, recording):
    mid = engine.process(recording)
    hits = engine.store.search_segments("shafts")
    assert hits and hits[0]["meeting_id"] == mid and hits[0]["speaker"] == "S1"
    assert engine.store.search_segments("الشافت")
    assert engine.store.search_segments('weird "quote (paren')  == []
    assert engine.store.items(query="facade")[0]["kind"] == "decision"


def test_failed_processing_is_recorded(engine, settings, recording):
    class Broken:
        def transcribe(self, audio, progress=None):
            raise RuntimeError("boom")

    engine.transcriber = Broken()
    with pytest.raises(RuntimeError):
        engine.process(recording)
    m = engine.store.meetings()[0]
    assert m["status"] == "error" and "boom" in m["error"]
    assert recording.exists()


def test_job_queue(engine, recording):
    jobs = JobQueue(engine)
    job = jobs.submit("process", str(recording), title="Weekly coordination", project="PRJ01", day="2026-10-27")
    jobs.wait()
    assert job.status == "done", job.error
    assert job.meeting_id == "2026-10-27_weekly-coordination"
    second = jobs.submit("process", "/nope.wav")
    jobs.wait()
    assert second.status == "error"


def test_guess_date(tmp_path):
    f = tmp_path / "2026-03-05 14-30-12.mkv"
    f.write_bytes(b"")
    assert guess_date(f) == "2026-03-05"


def test_cli_export_and_claude_config(engine, settings, recording, tmp_path, capsys):
    engine.process(recording)
    out = tmp_path / "register.csv"
    assert main(["export", str(out)]) == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert len(rows) == 3 and "Omar" not in out.read_text(encoding="utf-8-sig")
    assert main(["export", str(out), "--real-names"]) == 0
    assert "Marina Tower" in out.read_text(encoding="utf-8-sig")

    assert main(["claude-config"]) == 0
    assert '"coordy"' in capsys.readouterr().out
