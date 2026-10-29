"""Register Coord as an MCP server in Claude Desktop's config file."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def config_paths(existing_only: bool = False) -> list[Path]:
    candidates: list[Path] = []
    if sys.platform == "win32":
        if os.environ.get("APPDATA"):
            candidates.append(Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            # Microsoft Store / MSIX install keeps a virtualised copy of AppData.
            candidates += Path(local, "Packages").glob("Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library/Application Support/Claude/claude_desktop_config.json")
    else:
        candidates.append(Path.home() / ".config/Claude/claude_desktop_config.json")
    if existing_only:
        return [p for p in candidates if p.exists()]
    existing = [p for p in candidates if p.exists()]
    return existing or candidates[:1]


def server_entry() -> dict:
    entry: dict = {"command": sys.executable, "args": ["-m", "coordy", "mcp"]}
    if os.environ.get("COORDY_HOME"):
        entry["env"] = {"COORDY_HOME": os.environ["COORDY_HOME"]}
    return entry


def snippet() -> str:
    return json.dumps({"mcpServers": {"coordy": server_entry()}}, indent=2)


def write() -> list[Path]:
    written = []
    for path in config_paths():
        data = {}
        if path.exists():
            backup = path.with_suffix(f".backup-{datetime.now():%Y%m%d-%H%M%S}.json")
            shutil.copy2(path, backup)
            text = path.read_text(encoding="utf-8").strip()
            data = json.loads(text) if text else {}
        data.setdefault("mcpServers", {})["coordy"] = server_entry()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        written.append(path)
    return written
