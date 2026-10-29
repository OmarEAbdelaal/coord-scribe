"""`coordy doctor`: check every part of the local setup and say how to fix it."""

from __future__ import annotations

import importlib.util
import json
import platform
import sys

from .claude_desktop import config_paths
from .config import Settings
from .gpu import cuda_device_count, cuda_dll_dirs
from .llm import Ollama, is_local
from .store import Store

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def _line(status: str, what: str, hint: str = "") -> tuple[str, str, str]:
    print(f"{status} {what}")
    if hint and status != OK:
        print(f"       -> {hint}")
    return status, what, hint


def whisper_cached(model: str) -> bool:
    try:
        from faster_whisper.utils import download_model

        download_model(model, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def run(settings: Settings) -> int:
    results = []
    print(f"Coord home: {settings.paths.root}\n")
    results.append(_line(OK if sys.version_info >= (3, 11) else FAIL, f"Python {platform.python_version()}", "Use Python 3.12"))

    # Whisper / GPU
    gpus = cuda_device_count()
    if gpus:
        results.append(_line(OK, f"CUDA GPU visible to faster-whisper ({gpus})"))
    else:
        dirs = cuda_dll_dirs()
        results.append(_line(
            WARN,
            "No CUDA GPU for faster-whisper - transcription will run on CPU (slower)",
            "Update the NVIDIA driver; run scripts/install.ps1 again (installs CUDA 12 libraries)"
            + (f"; DLL folders found: {', '.join(map(str, dirs))}" if dirs else "; no CUDA DLL folders found"),
        ))
    model = settings.whisper.model
    cached = whisper_cached(model)
    results.append(_line(
        OK if cached else WARN,
        f"Whisper model '{model}' " + ("downloaded" if cached else "not downloaded yet"),
        "Run: coordy setup-models",
    ))

    # Speaker detection
    if settings.diarization.enabled:
        if importlib.util.find_spec("pyannote") is None:
            results.append(_line(WARN, "Speaker detection: pyannote.audio not installed", "Run scripts/install.ps1 (diarize extra)"))
        else:
            try:
                import torch

                cuda = torch.cuda.is_available()
                name = torch.cuda.get_device_name(0) if cuda else "CPU only"
                results.append(_line(OK if cuda else WARN, f"PyTorch {torch.__version__} ({name})", "Install the CUDA build of torch (scripts/install.ps1)"))
            except Exception as exc:  # noqa: BLE001
                results.append(_line(FAIL, f"PyTorch import failed: {exc}", "Re-run scripts/install.ps1"))
            has_token = bool(settings.hf_token or settings.privacy.offline)
            results.append(_line(
                OK if has_token else WARN,
                "Hugging Face token " + ("set" if has_token else "missing") + " (one-time speaker model download)",
                "Accept the model terms on huggingface.co, then set diarization.hf_token in config.toml",
            ))
    else:
        results.append(_line(OK, "Speaker detection disabled in config.toml"))

    # Local LLM
    host = settings.llm.host
    if not is_local(host) and not settings.privacy.allow_remote_llm:
        results.append(_line(FAIL, f"LLM host {host} is not local", "Use http://127.0.0.1:11434"))
    else:
        try:
            llm = Ollama(settings.llm, settings.privacy)
            results.append(_line(OK, f"Ollama {llm.version()} running at {host}"))
            results.append(_line(
                OK if llm.has_model() else FAIL,
                f"Ollama model '{settings.llm.model}' installed",
                f"Run: ollama pull {settings.llm.model}",
            ))
        except Exception:  # noqa: BLE001
            results.append(_line(FAIL, f"Ollama not reachable at {host}", "Install Ollama from ollama.com and start it"))

    # Storage and redaction
    store = Store(settings.paths.db)
    results.append(_line(
        OK if store.fts else WARN,
        "SQLite full-text search " + ("available" if store.fts else "unavailable"),
        "Search falls back to simple matching",
    ))
    try:
        from .redact import load_aliases

        n = len(load_aliases(settings.paths.aliases))
        results.append(_line(OK if n > 1 else WARN, f"{n} aliases in aliases.toml", f"Add your team, client and project names to {settings.paths.aliases}"))
    except Exception as exc:  # noqa: BLE001
        results.append(_line(FAIL, f"aliases.toml: {exc}", "Fix the file"))

    # Claude Desktop
    registered = False
    for path in config_paths(existing_only=True):
        try:
            registered |= "coordy" in json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})
        except Exception:  # noqa: BLE001
            pass
    results.append(_line(
        OK if registered else WARN,
        "Coord " + ("registered" if registered else "not registered") + " in Claude Desktop",
        "Run: coordy claude-config --write, then restart Claude Desktop",
    ))
    offline = settings.privacy.offline
    results.append(_line(
        OK if offline else WARN,
        "Offline mode " + ("on" if offline else "off") + " (blocks model downloads)",
        "After setup-models, set privacy.offline = true",
    ))

    fails = sum(1 for r in results if r[0] == FAIL)
    print(f"\n{fails} problem(s) to fix." if fails else "\nReady.")
    return 1 if fails else 0
