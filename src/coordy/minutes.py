"""Turn a (redacted) transcript into decisions, actions, risks and open questions."""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError

from .llm import LLMError, Ollama

log = logging.getLogger(__name__)

# Minutes list name -> item kind stored in the database.
SECTIONS = {"decisions": "decision", "actions": "action", "risks": "risk", "open_questions": "question"}


class Item(BaseModel):
    text: str
    owner: str = ""
    due: str = ""
    discipline: str = ""
    timestamp: str = ""


class Minutes(BaseModel):
    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    decisions: list[Item] = Field(default_factory=list)
    actions: list[Item] = Field(default_factory=list)
    risks: list[Item] = Field(default_factory=list)
    open_questions: list[Item] = Field(default_factory=list)


CONSOLIDATE = """You receive partial minutes extracted from consecutive parts of ONE meeting, as JSON.
Merge them into a single set of minutes: remove duplicates, keep the most specific wording,
keep codes (P01, ORG01...) and timestamps exactly, and write one summary for the whole meeting.
Do not add anything that is not in the input."""


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def transcript_lines(segments: list[dict], speaker_map: dict[str, str] | None = None) -> list[str]:
    speaker_map = speaker_map or {}
    lines = []
    for seg in segments:
        spk = speaker_map.get(seg.get("speaker", ""), seg.get("speaker", "")) or "?"
        lines.append(f"[{fmt_ts(seg['start'])}] {spk}: {seg['text']}")
    return lines


def chunk_lines(lines: list[str], max_chars: int, overlap: int = 3) -> list[str]:
    """Split on line boundaries; a few lines of overlap keep context across chunks."""
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if size + len(line) > max_chars and chunks[-1]:
            chunks.append(chunks[-1][-overlap:] if overlap else [])
            size = sum(len(x) + 1 for x in chunks[-1])
        chunks[-1].append(line)
        size += len(line) + 1
    return ["\n".join(c) for c in chunks if c]


def _ask(llm: Ollama, system: str, user: str) -> Minutes:
    schema = Minutes.model_json_schema()
    last: Exception | None = None
    for _ in range(2):
        try:
            return Minutes.model_validate(llm.chat_json(system, user, schema))
        except (LLMError, ValidationError) as exc:
            if isinstance(exc, LLMError) and "not installed" in str(exc):
                raise
            last = exc
            log.warning("Minutes extraction attempt failed: %s", exc)
    raise LLMError(f"Could not extract minutes: {last}")


def extract_minutes(llm: Ollama, system_prompt: str, lines: list[str], max_chars: int, title: str = "", progress=None) -> Minutes:
    chunks = chunk_lines(lines, max_chars)
    if not chunks:
        return Minutes(summary="(empty transcript)")
    header = f"Meeting: {title}\n\n" if title else ""
    parts: list[Minutes] = []
    for i, chunk in enumerate(chunks, 1):
        note = f"(part {i} of {len(chunks)})\n" if len(chunks) > 1 else ""
        parts.append(_ask(llm, system_prompt, f"{header}{note}Transcript:\n{chunk}"))
        if progress:
            progress(i / (len(chunks) + (1 if len(chunks) > 1 else 0)))
    if len(parts) == 1:
        return parts[0]
    merged_input = json.dumps([p.model_dump() for p in parts], ensure_ascii=False)
    try:
        return _ask(llm, system_prompt + "\n\n" + CONSOLIDATE, header + merged_input)
    except LLMError:
        log.warning("Consolidation failed; concatenating partial minutes")
        return concat(parts)


def concat(parts: list[Minutes]) -> Minutes:
    out = Minutes(summary=" ".join(p.summary for p in parts if p.summary))
    for p in parts:
        out.topics += [t for t in p.topics if t not in out.topics]
        for name in SECTIONS:
            getattr(out, name).extend(getattr(p, name))
    return out


def render_markdown(meeting: dict, minutes: dict, items: list[dict]) -> str:
    """Minutes of meeting as Markdown. `items` are the database rows (with ids and status)."""
    lines = [
        f"# {meeting.get('title') or meeting['id']}",
        "",
        f"- **Date:** {meeting.get('date', '')}",
        f"- **Project:** {meeting.get('project') or '-'}",
        f"- **Meeting ID:** {meeting['id']}",
        "",
        "## Summary",
        "",
        minutes.get("summary", "") or "-",
        "",
    ]
    if minutes.get("topics"):
        lines += ["## Topics", "", *[f"- {t}" for t in minutes["topics"]], ""]
    titles = {"decision": "Decisions", "action": "Actions", "risk": "Risks", "question": "Open questions / RFIs"}
    for kind, heading in titles.items():
        rows = [i for i in items if i["kind"] == kind]
        lines += [f"## {heading}", ""]
        if not rows:
            lines += ["- None recorded", ""]
            continue
        lines += ["| # | Item | Owner | Due | Discipline | Status | Time |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            cells = [str(r["id"]), r["text"], r["owner"], r["due"], r["discipline"], r["status"], r["timestamp"]]
            lines.append("| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |")
        lines.append("")
    return "\n".join(lines)
