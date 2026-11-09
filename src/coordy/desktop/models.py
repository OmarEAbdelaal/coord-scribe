"""Download official weights to the user's cache, never into Git or the installer."""
from pathlib import Path
import importlib.util

WHISPER_REPO = "Systran/faster-whisper-large-v3"


def install_models(settings, progress=print):
    if settings.privacy.offline:
        raise ValueError("Disable privacy.offline in config.toml before downloading models")
    from faster_whisper.utils import download_model
    model = settings.whisper.model
    progress(f"Downloading Whisper {model} (large-v3 is approximately 3 GB)…")
    folder = model if Path(model).is_dir() else download_model(model)
    required = ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json")
    if not all((Path(folder) / name).is_file() for name in required):
        raise RuntimeError("Whisper download is incomplete")
    progress("Checking local Ollama…")
    from ..llm import Ollama
    client = Ollama(settings.llm, settings.privacy)
    if not client.has_model():
        progress(f"Downloading {settings.llm.model} through local Ollama…")
        client.pull(progress=lambda event: progress(event.get("status", "Downloading…")))
    if not client.has_model():
        raise RuntimeError("Ollama model is still unavailable")
    if settings.diarization.enabled and importlib.util.find_spec("pyannote") is not None:
        progress("Downloading configured speaker model…")
        from ..diarize import Diarizer
        Diarizer(settings.diarization, settings.hf_token).pipeline()
    elif settings.diarization.enabled:
        progress("Optional speaker model skipped: install the diarize extra to enable speaker labeling.")
    progress("Models are available. Run doctor to check optional GPU and speaker dependencies.")
    return folder
