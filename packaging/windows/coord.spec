# Build on Windows: uv run --no-sync pyinstaller packaging/windows/coord.spec --noconfirm
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).parent.parent
assets = root / 'src' / 'coordy' / 'desktop' / 'assets'
datas = [(str(assets), 'coordy/desktop/assets'),
         (str(root / 'src' / 'coordy' / 'templates'), 'coordy/templates')]
binaries = []
hiddenimports = ['pyaudiowpatch', 'pycaw.pycaw', 'comtypes', 'scipy.signal']
# Native inference libraries, tokenizer assets, VAD models and browser driver are required at runtime.
for package in ['faster_whisper', 'ctranslate2', 'onnxruntime', 'tokenizers', 'playwright']:
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
for package in ['coordy', 'huggingface-hub']:
    datas += copy_metadata(package)
hiddenimports += collect_submodules('coordy')
a = Analysis([str(root / 'packaging' / 'windows' / 'desktop_entry.py')],
             pathex=[str(root / 'src')], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=['PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtPdf', 'torch', 'torchaudio', 'pyannote'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Coord', console=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Coord')
