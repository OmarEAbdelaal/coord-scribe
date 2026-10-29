"""Paths and settings. Everything lives under one folder (default: ~/Coordy)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".mkv", ".webm", ".ogg", ".flac", ".aac", ".wma", ".mov"}

TEMPLATE_FILES = ("config.toml", "aliases.toml", "glossary.txt", "minutes_prompt.txt")


def home_dir() -> Path:
    return Path(os.environ.get("COORDY_HOME") or Path.home() / "Coordy").expanduser()


@dataclass
class Paths:
    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def audio(self) -> Path:
        return self.root / "audio"

    @property
    def raw(self) -> Path:
        """Unredacted transcripts. Never read by the MCP server."""
        return self.root / "raw"

    @property
    def redacted(self) -> Path:
        return self.root / "redacted"

    @property
    def minutes(self) -> Path:
        return self.root / "minutes"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def db(self) -> Path:
        return self.root / "coordy.db"

    @property
    def config(self) -> Path:
        return self.root / "config.toml"

    @property
    def aliases(self) -> Path:
        return self.root / "aliases.toml"

    @property
    def glossary(self) -> Path:
        return self.root / "glossary.txt"

    @property
    def minutes_prompt(self) -> Path:
        return self.root / "minutes_prompt.txt"

    def dirs(self) -> list[Path]:
        return [self.inbox, self.audio, self.raw, self.redacted, self.minutes, self.logs]


@dataclass
class WhisperSettings:
    model: str = "large-v3"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = ""
    multilingual: bool = True
    beam_size: int = 5
    vad: bool = True


@dataclass
class DiarizationSettings:
    enabled: bool = True
    model: str = "pyannote/speaker-diarization-community-1"
    hf_token: str = ""
    min_speakers: int = 0
    max_speakers: int = 0


@dataclass
class LLMSettings:
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3:14b"
    num_ctx: int = 32768
    temperature: float = 0.1
    think: bool = False
    chunk_chars: int = 20000
    minutes_language: str = "English"
    timeout_seconds: float = 900


@dataclass
class PrivacySettings:
    offline: bool = False
    allow_remote_llm: bool = False
    redact_emails: bool = True
    redact_phones: bool = True


@dataclass
class Settings:
    paths: Paths
    whisper: WhisperSettings = field(default_factory=WhisperSettings)
    diarization: DiarizationSettings = field(default_factory=DiarizationSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)

    @property
    def hf_token(self) -> str:
        return self.diarization.hf_token or os.environ.get("HF_TOKEN", "")

    def glossary(self) -> str:
        if self.paths.glossary.exists():
            return self.paths.glossary.read_text(encoding="utf-8").strip()
        return ""

    def minutes_prompt(self) -> str:
        path = self.paths.minutes_prompt
        text = path.read_text(encoding="utf-8") if path.exists() else template_text("minutes_prompt.txt")
        return text.replace("{language}", self.llm.minutes_language)


def template_text(name: str) -> str:
    return resources.files("coordy.templates").joinpath(name).read_text(encoding="utf-8")


def _apply(target: Any, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if hasattr(target, key):
            setattr(target, key, type(getattr(target, key))(value))


def init_home(root: Path | None = None) -> Paths:
    """Create the folder layout and copy editable templates (never overwrites)."""
    paths = Paths(root or home_dir())
    for d in [paths.root, *paths.dirs()]:
        d.mkdir(parents=True, exist_ok=True)
    for name in TEMPLATE_FILES:
        target = paths.root / name
        if not target.exists():
            target.write_text(template_text(name), encoding="utf-8")
    return paths


def load_settings(root: Path | None = None) -> Settings:
    paths = init_home(root)
    settings = Settings(paths=paths)
    data = tomllib.loads(paths.config.read_text(encoding="utf-8"))
    for section in ("whisper", "diarization", "llm", "privacy"):
        _apply(getattr(settings, section), data.get(section, {}))
    apply_privacy_env(settings)
    return settings


def apply_privacy_env(settings: Settings) -> None:
    """Switch off third-party telemetry and, in offline mode, all hub downloads."""
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")
    if settings.privacy.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
