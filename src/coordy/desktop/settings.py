"""Non-secret desktop preferences; credentials stay in environment variables."""
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path


@dataclass
class DesktopSettings:
    microphone: bool = True
    speaker_device: str = ""
    microphone_device: str = ""
    auto_start: bool = False
    auto_stop_seconds: int = 90
    translation: str = "none"
    caption_chunk_seconds: int = 4
    caption_model: str = ""
    auto_send: bool = False
    delivery: str = "teams"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    sender: str = ""
    recipients: str = ""

    @classmethod
    def load(cls, root: Path):
        path = root / "desktop.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def save(self, root: Path):
        path = root / "desktop.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        temporary.replace(path)
