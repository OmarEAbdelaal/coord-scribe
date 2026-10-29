"""MCP server for Claude Desktop.

Privacy contract: every tool reads only the redacted database and redacted files.
Raw transcripts (raw/) and the real names in aliases.toml are never returned, and
all output passes through the redactor once more before it leaves this process.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .config import AUDIO_EXTENSIONS, Settings, load_settings
from .minutes import transcript_lines
from .pipeline import Engine, JobQueue, inbox_files
from .store import ITEM_KINDS, ITEM_STATUSES

log = logging.getLogger(__name__)

INSTRUCTIONS = """Coord is a private, local meeting-intelligence store for an AECO design team.
People, companies and projects appear as codes (P01, ORG01, PRJ01). Call list_codes to see each
code's role. Never ask the user for real names and never try to guess them; keep codes in your output.
Workflow: list_inbox -> process_recording -> job_status (transcription of a 1-hour meeting takes a
few minutes) -> get_minutes / list_items / search. Use update_item to close or reassign actions."""


def build_server(settings: Settings | None = None, engine: Engine | None = None) -> MCPServer:
    settings = settings or load_settings()
    engine = engine or Engine(settings)
    jobs = JobQueue(engine)
    store = engine.store
    mcp = MCPServer("coordy", title="Coord", instructions=INSTRUCTIONS)

    def safe(value):
        return engine.redactor().guard(value)

    def meeting_or_error(meeting_id: str) -> dict:
        meeting = store.meeting(meeting_id)
        if meeting is None:
            raise ValueError(f"No meeting {meeting_id!r}. Use list_meetings.")
        return meeting

    def resolve_audio(file: str) -> Path:
        path = Path(file)
        if not path.is_absolute():
            path = settings.paths.inbox / file
        if not path.is_file():
            raise ValueError(f"File not found: {file}. Use list_inbox, or give the full path.")
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(f"Not an audio/video file: {path.name}")
        return path

    @mcp.tool()
    def list_codes() -> list[dict]:
        """Codes used instead of real names (people, organisations, projects), with their roles."""
        return engine.redactor().public_codes()

    @mcp.tool()
    def list_inbox() -> list[dict]:
        """Recordings waiting in the Coordy inbox folder that have not been processed yet."""
        return [
            {"file": p.name, "size_mb": round(p.stat().st_size / 1e6, 1)} for p in inbox_files(settings, store)
        ]

    @mcp.tool()
    def process_recording(file: str, title: str = "", project: str = "", date: str = "") -> dict:
        """Start transcribing a recording locally (Whisper + speaker detection + redaction + minutes).

        file: a file name from list_inbox, or a full path. date: YYYY-MM-DD (default: from the file).
        Returns a job id immediately; poll job_status until it is done.
        """
        path = resolve_audio(file)
        job = jobs.submit("process", str(path), title=title, project=project, day=date)
        return safe(job.public())

    @mcp.tool()
    def job_status(job_id: str = "") -> list[dict]:
        """Progress of processing jobs started in this session (all jobs if job_id is empty)."""
        selected = [jobs.jobs[job_id]] if job_id in jobs.jobs else list(jobs.jobs.values())
        return safe([j.public() for j in selected])

    @mcp.tool()
    def list_meetings(project: str = "", limit: int = 20) -> list[dict]:
        """Processed meetings, newest first."""
        keep = ("id", "title", "project", "date", "duration", "language", "status", "error", "summary")
        return safe([{k: m[k] for k in keep} for m in store.meetings(project=project, limit=limit)])

    @mcp.tool()
    def get_minutes(meeting_id: str) -> dict:
        """Summary, topics and all decisions / actions / risks / open questions (with item ids)."""
        meeting = meeting_or_error(meeting_id)
        minutes = meeting["minutes"] or {}
        return safe(
            {
                "meeting_id": meeting_id,
                "title": meeting["title"],
                "date": meeting["date"],
                "project": meeting["project"],
                "speakers": meeting["speakers"],
                "summary": minutes.get("summary", ""),
                "topics": minutes.get("topics", []),
                "items": store.items(meeting_id=meeting_id),
            }
        )

    @mcp.tool()
    def get_transcript(meeting_id: str, offset: int = 0, limit: int = 400) -> dict:
        """Redacted transcript lines "[hh:mm:ss] SPEAKER: text". Page with offset/limit."""
        meeting = meeting_or_error(meeting_id)
        lines = transcript_lines(store.segments(meeting_id, offset, limit), meeting["speakers"])
        total = store.segment_count(meeting_id)
        return safe({"meeting_id": meeting_id, "offset": offset, "total_lines": total, "lines": lines})

    @mcp.tool()
    def search(query: str, project: str = "", limit: int = 30) -> dict:
        """Search all transcripts and items (decisions, actions, risks, questions)."""
        return safe(
            {
                "transcript_hits": store.search_segments(query, project=project, limit=limit),
                "item_hits": store.items(query=query, project=project, limit=limit),
            }
        )

    @mcp.tool()
    def list_items(
        kind: str = "", status: str = "open", project: str = "", owner: str = "", meeting_id: str = "", limit: int = 200
    ) -> list[dict]:
        """The project register across meetings.

        kind: decision | action | risk | question (empty = all). status: open | in_progress | closed |
        superseded (empty = all). owner: a code such as P02.
        """
        return safe(store.items(meeting_id=meeting_id, kind=kind, status=status, project=project, owner=owner, limit=limit))

    @mcp.tool()
    def add_item(meeting_id: str, kind: str, text: str, owner: str = "", due: str = "", discipline: str = "") -> dict:
        """Add a decision, action, risk or question by hand. kind: decision | action | risk | question."""
        if kind not in ITEM_KINDS:
            raise ValueError(f"kind must be one of {ITEM_KINDS}")
        meeting_or_error(meeting_id)
        item = engine.redactor().guard(
            {"kind": kind, "text": text, "owner": owner, "due": due, "discipline": discipline}
        )
        item_id = store.add_item(meeting_id, item)
        engine.write_minutes(meeting_id)
        return {"id": item_id}

    @mcp.tool()
    def update_item(item_id: int, status: str = "", owner: str = "", due: str = "", note: str = "") -> dict:
        """Update an item. status: open | in_progress | closed | superseded."""
        if status and status not in ITEM_STATUSES:
            raise ValueError(f"status must be one of {ITEM_STATUSES}")
        fields = engine.redactor().guard({"status": status, "owner": owner, "due": due, "note": note})
        item = store.update_item(item_id, **fields)
        if item is None:
            raise ValueError(f"No item {item_id}")
        engine.write_minutes(item["meeting_id"])
        return safe(item)

    @mcp.tool()
    def label_speakers(meeting_id: str, mapping: dict[str, str]) -> dict:
        """Map detected speakers to codes, e.g. {"S1": "P01", "S2": "P03"}. Only codes from list_codes."""
        meeting_or_error(meeting_id)
        return {"speakers": engine.set_speakers(meeting_id, mapping)}

    @mcp.tool()
    def regenerate_minutes(meeting_id: str) -> dict:
        """Re-run local minutes extraction (e.g. after label_speakers). Hand-edited items are kept."""
        meeting_or_error(meeting_id)
        return safe(jobs.submit("summarise", meeting_id).public())

    @mcp.prompt()
    def minutes_of_meeting(meeting_id: str) -> str:
        """Formal minutes of meeting for one meeting."""
        return (
            f"Use get_minutes for meeting {meeting_id} and list_codes. Write formal minutes of meeting for an "
            "AECO design coordination meeting: header (project, date), attendees by code and role, summary, "
            "decisions, action register (owner, due date, status), risks, open questions/RFIs. Keep all codes. "
            "Check the transcript (get_transcript) wherever an item is ambiguous."
        )

    @mcp.prompt()
    def decision_register(project: str = "") -> str:
        """Cross-meeting decision and action register for a project."""
        return (
            f"Use list_items (status empty, project {project!r}) and list_meetings. Build a project decision "
            "register and an open-actions tracker grouped by discipline. Flag decisions that contradict or "
            "supersede earlier ones, actions overdue or without owner, and recurring risks."
        )

    return mcp


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    handler = logging.FileHandler(settings.paths.logs / "mcp.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    build_server(settings).run("stdio")
