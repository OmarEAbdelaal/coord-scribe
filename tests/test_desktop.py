"""Platform-independent desktop behavior; Windows hardware is tested separately."""
import json
from pathlib import Path
import sqlite3
import threading
from unittest.mock import MagicMock, Mock

import httpx
import numpy as np
import pytest

from coordy.desktop.capture import mix, mono_pcm
from coordy.desktop.delivery import Delivery
from coordy.desktop.detection import Activity, MeetingDetector, classify
from coordy.desktop.guest import provider_for
from coordy.desktop.session import CaptureSession
from coordy.desktop.settings import DesktopSettings
from coordy.transcribe import Segment
from tests.conftest import write_wav, FakeOllama


def test_detection_requires_evidence_and_debounces():
    assert classify('chrome.exe', 'Email - Chrome', True) is None
    assert classify('zoom.exe', 'Zoom', False) is None
    assert classify('ms-teams.exe', 'Team sync', True)
    assert classify('chrome.exe', 'Google Meet - Project', True)
    assert classify('POWERPNT.EXE', 'PowerPoint Slide Show - Project', False)
    clock = [0.0]
    detector = MeetingDetector(90, clock=lambda: clock[0])
    activity = [Activity('Teams', 'Meeting')]
    assert detector.update(activity) is None
    assert detector.update(activity) == 'start'
    clock[0] = 89
    assert detector.update([]) is None
    clock[0] = 90
    assert detector.update([]) == 'stop'
    assert detector.update(activity) is None
    assert detector.update(activity) == 'start'


@pytest.mark.parametrize('url,provider', [
    ('https://teams.microsoft.com/l/meetup-join/abc', 'Teams'),
    ('https://meet.google.com/abc-defg-hij', 'Google Meet'),
    ('https://us02web.zoom.us/j/12345', 'Zoom'),
])
def test_guest_providers(url, provider):
    assert provider_for(url) == provider


@pytest.mark.parametrize('url', ['http://meet.google.com/x', 'https://zoom.us.attacker.org/j/1',
    'file:///tmp/a', 'https://user:pass@teams.microsoft.com/x', 'https://teams.microsoft.com:8080/x'])
def test_guest_rejects_unrelated_or_credential_urls(url):
    with pytest.raises(ValueError):
        provider_for(url)


def test_audio_mix_and_resample():
    # A one-second stereo signal at 48 kHz becomes 16 kHz mono, preserving amplitude.
    pcm = np.full((48000, 2), 8192, dtype=np.int16).tobytes()
    mono = mono_pcm(pcm, 2, 48000)
    assert mono.shape == (16000,)
    assert mono[100:-100].mean() == pytest.approx(0.25, abs=1e-3)
    assert np.max(np.abs(mix(mono, mono))) <= 1
    assert np.allclose(mix(mono, -mono), 0)
    with pytest.raises(RuntimeError):
        mix(mono, mono[:10])


def test_desktop_preferences_preserve_secret_separation(tmp_path):
    preferences = DesktopSettings()
    assert not preferences.auto_start and not preferences.auto_send
    preferences.recipients = 'pm@example.com'
    preferences.save(tmp_path)
    assert DesktopSettings.load(tmp_path) == preferences
    assert 'password' not in (tmp_path / 'desktop.json').read_text()


def test_teams_card_and_durable_duplicate_protection(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDY_TEAMS_WEBHOOK', 'https://example.environment.api.powerplatform.com/test')
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(202)
    preferences = DesktopSettings()
    transport = httpx.MockTransport(respond)
    assert Delivery(tmp_path, preferences, transport).send('m1', 'Summary: P01 agreed.', 'teams') == 'Sent'
    assert Delivery(tmp_path, preferences, transport).send('m1', 'Summary: P01 agreed.', 'teams') == 'Already sent'
    assert len(requests) == 1
    assert requests[0]['attachments'][0]['content']['body'][0]['text'] == 'Summary: P01 agreed.'


def test_delivery_failure_requires_explicit_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDY_TEAMS_WEBHOOK', 'https://prod.logic.azure.com/webhook?sig=secret')
    preferences = DesktopSettings()
    failing = Delivery(tmp_path, preferences, httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(RuntimeError, match='Check configuration'):
        failing.send('m1', 'Minutes', 'teams')
    with pytest.raises(RuntimeError, match='needs review'):
        failing.send('m1', 'Minutes', 'teams')
    with sqlite3.connect(tmp_path / 'delivery.db') as db:
        assert 'secret' not in str(db.execute('SELECT * FROM deliveries').fetchall())
    succeeding = Delivery(tmp_path, preferences, httpx.MockTransport(lambda r: httpx.Response(200)))
    assert succeeding.send('m1', 'Minutes', 'teams', retry=True) == 'Sent'


def test_smtp_uses_tls_before_login_and_send(tmp_path, monkeypatch):
    smtp = Mock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = smtp
    monkeypatch.setattr('coordy.desktop.delivery.smtplib.SMTP', factory)
    monkeypatch.setenv('COORDY_SMTP_PASSWORD', 'test-only-secret')
    prefs = DesktopSettings(smtp_host='smtp.example.com', smtp_user='coord@example.com',
                            sender='coord@example.com', recipients='pm@example.com, team@example.com')
    Delivery(tmp_path, prefs).send('m1', 'Redacted minutes', 'email')
    methods = [call[0] for call in smtp.method_calls]
    assert methods.index('starttls') < methods.index('login') < methods.index('send_message')
    message = smtp.send_message.call_args.args[0]
    assert 'pm@example.com' in message['To']
    assert 'Redacted minutes' in message.get_content()


def test_live_session_preserves_timestamps_and_reuses_transcription(settings, engine):
    events = []
    class Capture:
        def stop(self):
            pass
        def record(self, path, on_chunk, on_ready):
            write_wav(path)
            on_ready()
            on_chunk(np.zeros(16000, np.float32), 0)
            on_chunk(np.zeros(16000, np.float32), 1)
    class Speech:
        def transcribe(self, audio):
            return [Segment(0, 0.5, 'Omar Elsayed: revise the shafts', language='en')]
    # No second transcription should happen during finalization.
    engine.transcriber = Mock()
    engine.transcriber.transcribe.side_effect = AssertionError('Unexpected retranscription')
    session = CaptureSession(settings, DesktopSettings(), lambda *event: events.append(event),
                             capture=Capture(), transcriber=Speech(), engine=engine)
    mid = session.run()
    assert engine.store.meeting(mid)['status'] == 'done'
    assert [s['start'] for s in engine.store.segments(mid)] == [0, 1]
    live = [value for kind, value in events if kind == 'transcript']
    assert all('Omar' not in item['text'] for item in live)
    assert 'P01' in live[0]['text']
    engine.transcriber.transcribe.assert_not_called()


def test_live_transcription_failure_keeps_audio(settings, engine):
    class Capture:
        def stop(self):
            pass
        def record(self, path, on_chunk, on_ready):
            write_wav(path)
            on_ready()
            on_chunk(np.zeros(16000, np.float32), 0)
    transcriber = Mock()
    transcriber.transcribe.side_effect = RuntimeError('Model unavailable')
    session = CaptureSession(settings, DesktopSettings(), lambda *args: None,
                             capture=Capture(), transcriber=transcriber, engine=engine)
    with pytest.raises(RuntimeError, match='recording retained'):
        session.run()
    assert len(list(settings.paths.inbox.glob('*.wav'))) == 1
    assert not engine.store.meetings()


def test_model_download_stream_errors_are_not_success(settings):
    from coordy.llm import LLMError, Ollama
    backend = httpx.MockTransport(lambda r: httpx.Response(200, text='{"error":"disk full"}\n'))
    client = Ollama(settings.llm, settings.privacy, transport=backend)
    with pytest.raises(LLMError, match='disk full'):
        client.pull()


def test_no_speech_does_not_generate_minutes(engine, recording):
    with pytest.raises(ValueError, match='No speech detected'):
        engine.process(recording, segments=[])
    assert engine.store.meetings()[0]['status'] == 'error'


def test_missing_optional_diarization_is_reported_cleanly(monkeypatch):
    import builtins
    from coordy.config import DiarizationSettings
    from coordy.diarize import Diarizer, DiarizationUnavailable
    original = builtins.__import__
    def without_torch(name, *args, **kwargs):
        if name == 'torch':
            raise ImportError('torch not installed')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_torch)
    with pytest.raises(DiarizationUnavailable, match='not installed'):
        Diarizer(DiarizationSettings()).diarize(np.zeros(16000, np.float32))


def test_setup_models_failure_exits_nonzero(monkeypatch):
    from coordy.cli import main
    from coordy.desktop import models
    monkeypatch.setattr(models, 'install_models', Mock(side_effect=RuntimeError('Download unavailable')))
    assert main(['setup-models']) == 1


@pytest.mark.parametrize('translation_fails', [False, True])
def test_live_caption_translation_switching_and_failure_isolation(settings, engine, fake_ollama, translation_fails):
    from tests.conftest import MINUTES
    events = []
    class Capture:
        def stop(self):
            pass
        def record(self, path, on_chunk, on_ready):
            write_wav(path)
            on_ready()
            on_chunk(np.zeros(16000, np.float32), 0)
            on_chunk(np.zeros(16000, np.float32), 1)
    class Speech:
        def transcribe(self, audio):
            return [Segment(0, .5, 'Omar Elsayed agreed', language='en')]
    def reply(body):
        if body['messages'][0]['content'].startswith('Translate'):
            return {} if translation_fails else {'text': 'Omar Elsayed وافق'}
        return MINUTES
    fake_ollama.reply = reply
    session = None
    def emit(kind, value):
        events.append((kind, value))
        if kind in {'translation', 'caption_translation_error'}:
            session.set_translation('Arabic')
    session = CaptureSession(settings, DesktopSettings(translation='English'), emit,
                             capture=Capture(), transcriber=Speech(), engine=engine)
    mid = session.run()
    assert engine.store.meeting(mid)['status'] == 'done'
    assert len([item for kind, item in events if kind == 'transcript']) == 2
    outcome = 'caption_translation_error' if translation_fails else 'translation'
    captions = [item for kind, item in events if kind == outcome]
    assert [item['language'] for item in captions] == ['English', 'Arabic']
    assert [item['start'] for item in captions] == [0, 1]
    if not translation_fails:
        saved = json.loads((settings.paths.redacted / f'{mid}.translation.json').read_text(encoding='utf-8'))
        assert saved == captions
        assert all('Omar' not in item['text'] and 'P01' in item['text'] for item in captions)
    else:
        assert session.failure is None
        assert not session.translations


def test_short_capture_reports_duration_without_loading_whisper(settings, engine):
    class Capture:
        def stop(self):
            pass
        def record(self, path, on_chunk, on_ready):
            write_wav(path, seconds=.1)
            on_chunk(np.zeros(1600, np.float32), 0)
    speech = Mock()
    session = CaptureSession(settings, DesktopSettings(), lambda *args: None,
                             capture=Capture(), transcriber=speech, engine=engine)
    with pytest.raises(RuntimeError, match='only 0.10 seconds'):
        session.run()
    speech.transcribe.assert_not_called()
