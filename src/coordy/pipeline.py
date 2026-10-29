"""Recording -> transcript -> speakers -> redaction -> minutes, plus a background job queue."""

from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from pathlib import Path

from .config import AUDIO_EXTENSIONS, Settings
from .diarize import DiarizationUnavailable, Diarizer, assign_speakers
from .llm import Ollama
from .minutes import SECTIONS, extract_minutes, render_markdown, transcript_lines
from .redact import Redactor
from .store import Store
from .transcribe import SAMPLE_RATE, Transcriber, load_audio

log = logging.getLogger(__name__)
DATE_IN_NAME = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:60].strip("-") or "meeting"


def guess_date(path: Path) -> str:
    m = DATE_IN_NAME.search(path.stem)
    if m:
        try:
            return Date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


class Engine:
    """Holds the loaded models so they are reused across meetings."""

    def __init__(self, settings: Settings, transcriber=None, diarizer=None, llm=None):
        self.settings = settings
        self.store = Store(settings.paths.db)
        self.transcriber = transcriber or Transcriber(settings.whisper, settings.glossary())
        self.diarizer = diarizer if diarizer is not None else (
            Diarizer(settings.diarization, settings.hf_token) if settings.diarization.enabled else None
        )
        self._llm = llm

    @property
    def llm(self) -> Ollama:
        if self._llm is None:
            self._llm = Ollama(self.settings.llm, self.settings.privacy)
        return self._llm

    def redactor(self) -> Redactor:
        # Re-read each time so edits to aliases.toml apply without a restart.
        return Redactor.from_settings(self.settings)

    def new_meeting_id(self, path: Path, title: str, day: str) -> str:
        base = f"{day}_{slugify(title or path.stem)}"
        mid, n = base, 2
        while self.store.meeting(mid):
            mid, n = f"{base}-{n}", n + 1
        return mid

    # ------------------------------------------------------------------
    def process(self, path: Path, title: str = "", project: str = "", day: str = "", stage=None) -> str:
        stage = stage or (lambda name, frac=None: None)
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(f"Not an audio/video file: {path.name}")
        paths = self.settings.paths
        day = day or guess_date(path)
        mid = self.new_meeting_id(path, title, day)
        title = title or path.stem
        self.store.upsert_meeting(mid, title=title, project=project, date=day, audio_path=str(path), status="transcribing")
        try:
            stage("decoding audio")
            audio = load_audio(path)
            duration = len(audio) / SAMPLE_RATE

            stage("transcribing", 0.0)
            segments = self.transcriber.transcribe(audio, progress=lambda f: stage("transcribing", f))
            languages = Counter(s.language for s in segments if s.language)

            if self.diarizer is not None:
                stage("detecting speakers")
                self.store.upsert_meeting(mid, status="diarizing")
                try:
                    assign_speakers(segments, self.diarizer.diarize(audio))
                except DiarizationUnavailable as exc:
                    log.warning("Speaker detection skipped: %s", exc)
            del audio

            # Raw transcript: real names, stays in raw/ only.
            raw = paths.raw / f"{mid}.json"
            raw.write_text(
                json.dumps({"meeting_id": mid, "audio": str(path), "segments": [s.to_dict() for s in segments]}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            if path.parent.resolve() == paths.inbox.resolve():
                moved = paths.audio / path.name
                if moved.exists():
                    moved = paths.audio / f"{mid}{path.suffix}"
                shutil.move(str(path), moved)
                path = moved

            self.store.upsert_meeting(
                mid,
                audio_path=str(path),
                duration=duration,
                language=",".join(lang for lang, _ in languages.most_common(2)),
            )
            stage("redacting")
            self.redact(mid)
            self.summarise(mid, stage=stage)
            self.store.upsert_meeting(mid, status="done", error="")
            stage("done", 1.0)
            return mid
        except Exception as exc:
            self.store.upsert_meeting(mid, status="error", error=str(exc)[:500])
            raise

    def redact(self, mid: str) -> dict:
        """(Re)build the redacted transcript from raw/ using the current aliases."""
        paths = self.settings.paths
        raw = json.loads((paths.raw / f"{mid}.json").read_text(encoding="utf-8"))
        redactor = self.redactor()
        hits: Counter = Counter()
        segments = [{**s, "text": redactor.redact(s["text"], hits)} for s in raw["segments"]]
        self.store.replace_segments(mid, segments)
        self.write_transcript(mid)
        return dict(hits)

    def write_transcript(self, mid: str) -> Path:
        meeting = self.store.meeting(mid)
        lines = transcript_lines(self.store.segments(mid), meeting["speakers"])
        out = self.settings.paths.redacted / f"{mid}.md"
        out.write_text(f"# {meeting['title']}\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return out

    def summarise(self, mid: str, stage=None) -> dict:
        stage = stage or (lambda name, frac=None: None)
        meeting = self.store.meeting(mid)
        self.store.upsert_meeting(mid, status="summarising")
        stage("extracting decisions and actions", 0.0)
        lines = transcript_lines(self.store.segments(mid), meeting["speakers"])
        minutes = extract_minutes(
            self.llm,
            self.settings.minutes_prompt(),
            lines,
            self.settings.llm.chunk_chars,
            title=meeting["title"],
            progress=lambda f: stage("extracting decisions and actions", f),
        )
        # Defence in depth: the model only saw redacted text, but redact its output too.
        data = self.redactor().guard(minutes.model_dump())
        items = [{**item, "kind": kind} for section, kind in SECTIONS.items() for item in data[section]]
        self.store.replace_llm_items(mid, items)
        self.store.upsert_meeting(mid, summary=data["summary"], minutes=json.dumps(data, ensure_ascii=False), status="done")
        self.write_minutes(mid)
        return data

    def write_minutes(self, mid: str) -> Path:
        meeting = self.store.meeting(mid)
        md = render_markdown(meeting, meeting["minutes"] or {}, self.store.items(meeting_id=mid))
        paths = self.settings.paths
        (paths.minutes / f"{mid}.json").write_text(
            json.dumps(meeting["minutes"] or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        out = paths.minutes / f"{mid}.md"
        out.write_text(md, encoding="utf-8")
        return out

    def write_named_minutes(self, mid: str) -> Path:
        """Minutes with real names restored. Local only: never exposed through MCP."""
        md = self.write_minutes(mid).read_text(encoding="utf-8")
        out = self.settings.paths.minutes / f"{mid}.named.md"
        out.write_text(self.redactor().unredact(md), encoding="utf-8")
        return out

    def set_speakers(self, mid: str, mapping: dict[str, str]) -> dict:
        redactor = self.redactor()
        bad = [code for code in mapping.values() if code not in redactor.codes]
        if bad:
            raise ValueError(f"Unknown codes {bad}. Use codes from aliases.toml: {sorted(redactor.codes)}")
        meeting = self.store.meeting(mid)
        if meeting is None:
            raise KeyError(mid)
        merged = {**meeting["speakers"], **mapping}
        self.store.set_speakers(mid, merged)
        self.write_transcript(mid)
        return merged


# ----------------------------------------------------------------------
@dataclass
class Job:
    id: str
    kind: str
    target: str
    status: str = "queued"
    stage: str = ""
    progress: float = 0.0
    meeting_id: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""

    def public(self) -> dict:
        return dict(self.__dict__)


class JobQueue:
    """One worker thread: the GPU handles one meeting at a time."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None

    def submit(self, kind: str, target: str, **kwargs) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], kind=kind, target=target)
        self.jobs[job.id] = job
        self._queue.put((job, kwargs))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, name="coordy-worker", daemon=True)
            self._thread.start()
        return job

    def _work(self) -> None:
        while True:
            job, kwargs = self._queue.get()

            def stage(name: str, frac: float | None = None, job: Job = job) -> None:
                job.stage = name
                if frac is not None:
                    job.progress = round(frac, 3)

            job.status = "running"
            try:
                if job.kind == "process":
                    job.meeting_id = self.engine.process(Path(job.target), stage=stage, **kwargs)
                elif job.kind == "summarise":
                    job.meeting_id = job.target
                    self.engine.summarise(job.target, stage=stage)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - reported through job_status
                log.exception("Job %s failed", job.id)
                job.status = "error"
                job.error = str(exc)[:500]
            finally:
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                self._queue.task_done()

    def wait(self) -> None:
        """Block until every submitted job has finished."""
        self._queue.join()


def inbox_files(settings: Settings, store: Store) -> list[Path]:
    done = store.processed_audio()
    return sorted(
        p
        for p in settings.paths.inbox.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS and str(p) not in done
    )

