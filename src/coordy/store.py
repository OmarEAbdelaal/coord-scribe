"""SQLite store. Holds REDACTED text only, so everything in it is safe for the MCP server."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ITEM_KINDS = ("decision", "action", "risk", "question")
ITEM_STATUSES = ("open", "in_progress", "closed", "superseded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    audio_path TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    error TEXT NOT NULL DEFAULT '',
    speakers TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    minutes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    speaker TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    PRIMARY KEY (meeting_id, idx)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    due TEXT NOT NULL DEFAULT '',
    discipline TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'llm',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_meeting ON items(meeting_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, meeting_id UNINDEXED, idx UNINDEXED, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)
            try:
                db.executescript(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # meetings -----------------------------------------------------------
    def upsert_meeting(self, id: str, **fields) -> None:
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM meetings WHERE id = ?", (id,)).fetchone()
            fields["updated_at"] = now()
            if exists:
                sets = ", ".join(f"{k} = ?" for k in fields)
                db.execute(f"UPDATE meetings SET {sets} WHERE id = ?", (*fields.values(), id))
            else:
                fields.setdefault("created_at", fields["updated_at"])
                cols = ", ".join(["id", *fields])
                marks = ", ".join("?" * (len(fields) + 1))
                db.execute(f"INSERT INTO meetings ({cols}) VALUES ({marks})", (id, *fields.values()))

    def meeting(self, id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM meetings WHERE id = ?", (id,)).fetchone()
        return _meeting(row) if row else None

    def meetings(self, project: str = "", limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM meetings"
        args: list = []
        if project:
            sql += " WHERE project LIKE ?"
            args.append(f"%{project}%")
        sql += " ORDER BY date DESC, created_at DESC LIMIT ?"
        with self.connect() as db:
            return [_meeting(r) for r in db.execute(sql, (*args, limit))]

    def processed_audio(self) -> set[str]:
        with self.connect() as db:
            return {r[0] for r in db.execute("SELECT audio_path FROM meetings WHERE status = 'done'")}

    def set_speakers(self, id: str, mapping: dict[str, str]) -> None:
        self.upsert_meeting(id, speakers=json.dumps(mapping))

    # segments -----------------------------------------------------------
    def replace_segments(self, meeting_id: str, segments: list[dict]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
            db.executemany(
                "INSERT INTO segments (meeting_id, idx, start, end, speaker, language, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (meeting_id, i, s["start"], s["end"], s.get("speaker", ""), s.get("language", ""), s["text"])
                    for i, s in enumerate(segments)
                ],
            )
            if self.fts:
                db.execute("DELETE FROM segments_fts WHERE meeting_id = ?", (meeting_id,))
                db.executemany(
                    "INSERT INTO segments_fts (text, meeting_id, idx) VALUES (?, ?, ?)",
                    [(s["text"], meeting_id, i) for i, s in enumerate(segments)],
                )

    def segments(self, meeting_id: str, offset: int = 0, limit: int = 100000) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT idx, start, end, speaker, language, text FROM segments WHERE meeting_id = ? ORDER BY idx LIMIT ? OFFSET ?",
                (meeting_id, limit, offset),
            )
            return [dict(r) for r in rows]

    def segment_count(self, meeting_id: str) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM segments WHERE meeting_id = ?", (meeting_id,)).fetchone()[0]

    def search_segments(self, query: str, project: str = "", limit: int = 30) -> list[dict]:
        base = (
            "SELECT s.meeting_id, s.idx, s.start, s.speaker, s.text, m.title, m.date, m.project, m.speakers "
            "FROM segments s JOIN meetings m ON m.id = s.meeting_id "
        )
        rows = []
        with self.connect() as db:
            if self.fts:
                try:
                    rows = db.execute(
                        "SELECT f.meeting_id, f.idx FROM segments_fts f WHERE segments_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_fts_query(query), limit * 3),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    keys = [(r["meeting_id"], r["idx"]) for r in rows]
                    found = []
                    for mid, idx in keys:
                        r = db.execute(
                            base + "WHERE s.meeting_id = ? AND s.idx = ? AND m.project LIKE ?",
                            (mid, idx, f"%{project}%"),
                        ).fetchone()
                        if r:
                            found.append(r)
                    return [_hit(r) for r in found[:limit]]
            like = db.execute(
                base + "WHERE s.text LIKE ? AND m.project LIKE ? ORDER BY m.date DESC, s.idx LIMIT ?",
                (f"%{query}%", f"%{project}%", limit),
            )
            return [_hit(r) for r in like]

    # items --------------------------------------------------------------
    def replace_llm_items(self, meeting_id: str, items: list[dict]) -> None:
        """Replace model-extracted items; items added or edited by hand are kept."""
        with self.connect() as db:
            db.execute("DELETE FROM items WHERE meeting_id = ? AND source = 'llm'", (meeting_id,))
            for item in items:
                _insert_item(db, meeting_id, item, source="llm")

    def add_item(self, meeting_id: str, item: dict) -> int:
        with self.connect() as db:
            return _insert_item(db, meeting_id, item, source="manual")

    def update_item(self, item_id: int, **fields) -> dict | None:
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        if "status" in fields and fields["status"] not in ITEM_STATUSES:
            raise ValueError(f"status must be one of {ITEM_STATUSES}")
        with self.connect() as db:
            if fields:
                fields["updated_at"] = now()
                fields["source"] = "manual"
                sets = ", ".join(f"{k} = ?" for k in fields)
                db.execute(f"UPDATE items SET {sets} WHERE id = ?", (*fields.values(), item_id))
            row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def items(
        self,
        meeting_id: str = "",
        kind: str = "",
        status: str = "",
        project: str = "",
        owner: str = "",
        query: str = "",
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT i.*, m.title AS meeting_title, m.date AS meeting_date, m.project FROM items i JOIN meetings m ON m.id = i.meeting_id WHERE 1 = 1"
        args: list = []
        for col, val in (("i.meeting_id", meeting_id), ("i.kind", kind), ("i.status", status), ("i.owner", owner)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        if project:
            sql += " AND m.project LIKE ?"
            args.append(f"%{project}%")
        if query:
            sql += " AND (i.text LIKE ? OR i.note LIKE ?)"
            args += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY m.date DESC, i.id LIMIT ?"
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, (*args, limit))]


def _insert_item(db, meeting_id: str, item: dict, source: str) -> int:
    kind = item.get("kind", "")
    if kind not in ITEM_KINDS:
        raise ValueError(f"kind must be one of {ITEM_KINDS}")
    ts = now()
    cur = db.execute(
        "INSERT INTO items (meeting_id, kind, text, owner, due, discipline, timestamp, status, note, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meeting_id,
            kind,
            item.get("text", "").strip(),
            item.get("owner", ""),
            item.get("due", ""),
            item.get("discipline", ""),
            item.get("timestamp", ""),
            item.get("status", "open") or "open",
            item.get("note", ""),
            source,
            ts,
            ts,
        ),
    )
    return cur.lastrowid


def _fts_query(query: str) -> str:
    # Quote each word so punctuation in user input cannot break FTS syntax.
    words = [w.replace('"', "") for w in query.split()]
    return " ".join(f'"{w}"' for w in words if w)


def _meeting(row) -> dict:
    d = dict(row)
    d["speakers"] = json.loads(d.get("speakers") or "{}")
    d["minutes"] = json.loads(d["minutes"]) if d.get("minutes") else None
    return d


def _hit(row) -> dict:
    speakers = json.loads(row["speakers"] or "{}")
    return {
        "meeting_id": row["meeting_id"],
        "meeting_title": row["title"],
        "date": row["date"],
        "project": row["project"],
        "line": row["idx"],
        "start": row["start"],
        "speaker": speakers.get(row["speaker"], row["speaker"]),
        "text": row["text"],
    }
