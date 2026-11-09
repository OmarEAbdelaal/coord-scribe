import gc
from types import SimpleNamespace
import weakref

from coordy import gpu
from coordy.config import WhisperSettings
from coordy.transcribe import Transcriber


def test_windows_cuda_directory_registration_stays_alive(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(gpu, '_done', False)
    monkeypatch.setattr(gpu, '_dll_handles', [])
    monkeypatch.setattr(gpu, 'cuda_dll_dirs', lambda: [tmp_path])
    monkeypatch.setenv('PATH', gpu.os.environ.get('PATH', ''))
    registrations = []
    class Registration:
        pass
    def register(path):
        handle = Registration()
        registrations.append(weakref.ref(handle))
        return handle
    monkeypatch.setattr(gpu.os, 'add_dll_directory', register, raising=False)
    assert gpu.prepare_cuda() == [tmp_path]
    gc.collect()
    assert registrations[0]() is not None
    gpu.prepare_cuda()
    assert len(registrations) == 1


def test_gpu_fallback_reports_cpu_to_desktop(monkeypatch):
    statuses = []
    transcriber = Transcriber(WhisperSettings(), status=statuses.append)
    transcriber._device = 'cuda'
    def run(audio, progress):
        if transcriber.device == 'cuda':
            raise RuntimeError('cublas64_12.dll missing')
        return []
    monkeypatch.setattr(transcriber, '_run', run)
    monkeypatch.setattr(transcriber, '_load', lambda device, compute: setattr(transcriber, '_device', device))
    assert transcriber.transcribe(None) == []
    assert any('switching Whisper to CPU' in message for message in statuses)
