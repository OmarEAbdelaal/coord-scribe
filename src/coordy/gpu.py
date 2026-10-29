"""Make the CUDA 12 DLLs that faster-whisper (CTranslate2) needs findable on Windows."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)
_done = False


def _torch_lib() -> tuple[Path, str] | None:
    """torch's bundled CUDA libs, without importing torch (slow)."""
    spec = importlib.util.find_spec("torch")
    if not spec or not spec.submodule_search_locations:
        return None
    root = Path(next(iter(spec.submodule_search_locations)))
    version_file = root / "version.py"
    cuda = ""
    if version_file.exists():
        # e.g.  cuda: Optional[str] = '12.8'
        m = re.search(r"^cuda\b[^=\n]*=\s*['\"]([\d.]+)['\"]", version_file.read_text(), re.M)
        cuda = m.group(1) if m else ""
    return root / "lib", cuda


def cuda_dll_dirs() -> list[Path]:
    dirs: list[Path] = []
    spec = importlib.util.find_spec("nvidia")
    if spec and spec.submodule_search_locations:
        for base in spec.submodule_search_locations:
            dirs += sorted(p for p in Path(base).glob("*/bin") if p.is_dir())
    torch = _torch_lib()
    # CTranslate2 is built against CUDA 12: only a CUDA 12 torch build has compatible DLLs.
    if torch and torch[1].startswith("12.") and torch[0].is_dir():
        dirs.append(torch[0])
    return dirs


def prepare_cuda() -> list[Path]:
    global _done
    if _done or sys.platform != "win32":
        return []
    _done = True
    dirs = cuda_dll_dirs()
    for d in dirs:
        os.add_dll_directory(str(d))
    if dirs:
        # CTranslate2 loads cuDNN lazily with LoadLibrary, which searches PATH.
        os.environ["PATH"] = os.pathsep.join([*map(str, dirs), os.environ.get("PATH", "")])
        log.info("CUDA DLL folders: %s", ", ".join(map(str, dirs)))
    return dirs


def cuda_device_count() -> int:
    prepare_cuda()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - any failure means "no usable GPU"
        return 0
