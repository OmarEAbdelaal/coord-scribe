"""Company server, low-latency captions, calendar reminders and the redesigned workspace."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import date, datetime, timedelta
import json

import httpx
import numpy as np
import pytest

from coordy.config import WhisperSettings
from coordy.desktop import calendar
from coordy.desktop.server import apply_server
from coordy.desktop.session import CaptureSession, RATE
from coordy.desktop.settings import DesktopSettings
from coordy.remote import RemoteTranscriber, ServerBlocked, is_private_host
from coordy.transcribe import Segment
from tests.conftest import write_wav


def test_speech_server_must_be_private_and_returns_segments():
    assert is_private_host('http://10.0.0.20:8000') and is_private_host('http://127.0.0.1:8000')
    assert not is_private_host('https://8.8.8.8')
    assert is_private_host('http://ai.apg.local', resolve=lambda host, port: [(0, 0, 0, '', ('192.168.1.5', 0))])
    assert not is_private_host('http://ai.example.com', resolve=lambda host, port: [(0, 0, 0, '', ('93.184.216.34', 0))])
    with pytest.raises(ServerBlocked):
        RemoteTranscriber(WhisperSettings(server='https://8.8.8.8'))
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'language': 'arabic', 'segments': [
            {'start': 0.0, 'end': 1.5, 'text': ' مرحبا '}, {'start': 1.5, 'end': 2, 'text': ' '}]})
    speech = RemoteTranscriber(WhisperSettings(server='http://10.0.0.20:8000/', server_model='whisper-1'), 'BOQ, tender',
                               transport=httpx.MockTransport(respond))
    segments = speech.transcribe(np.zeros(16000, np.float32))
    assert [(s.start, s.end, s.text, s.language) for s in segments] == [(0.0, 1.5, 'مرحبا', 'ar')]
    assert requests[0].url == 'http://10.0.0.20:8000/v1/audio/transcriptions'
    body = requests[0].content
    assert b'whisper-1' in body and b'BOQ, tender' in body and b'RIFF' in body
    failing = RemoteTranscriber(WhisperSettings(server='http://10.0.0.20:8000'), transport=httpx.MockTransport(lambda r: httpx.Response(500, text='busy')))
    with pytest.raises(RuntimeError, match='Speech server error 500'):
        failing.transcribe(np.zeros(16000, np.float32))


def test_desktop_server_settings_overlay_config_and_refuse_public_hosts(settings):
    apply_server(settings, DesktopSettings(llm_host='http://10.0.0.20:11434/', speech_server='http://10.0.0.20:8000'))
    assert settings.llm.host == 'http://10.0.0.20:11434' and settings.privacy.allow_remote_llm
    assert settings.whisper.server == 'http://10.0.0.20:8000'
    with pytest.raises(ValueError, match='private company network'):
        apply_server(settings, DesktopSettings(llm_host='https://8.8.8.8'))


class Scripted:
    """Speech fake: answers with the text spoken so far, from how much audio it is given."""
    def __init__(self):
        self.lengths = []

    def transcribe(self, audio):
        self.lengths.append(len(audio))
        seconds = len(audio) / RATE
        return [Segment(0, seconds, f'{seconds:.0f}s of speech', language='en')]


def run_session(settings, engine, blocks, speech=None):
    events = []
    class Capture:
        def stop(self):
            pass
        def record(self, path, on_chunk, on_ready):
            write_wav(path, seconds=len(blocks))
            for index, block in enumerate(blocks):
                on_chunk(block, float(index))
                session.chunks.join()  # real capture delivers one step per second
    session = CaptureSession(settings, DesktopSettings(), lambda *event: events.append(event),
                             capture=Capture(), transcriber=speech or Scripted(), engine=engine)
    return session, session.run(), events


def test_captions_stream_as_partials_then_settle_at_a_pause(settings, engine):
    loud = np.full(RATE, 0.2, np.float32)
    quiet = np.zeros(RATE, np.float32)
    speech = Scripted()
    session, mid, events = run_session(settings, engine, [loud, loud, quiet, loud], speech)
    # The open utterance is re-decoded as it grows, so text appears each second.
    assert speech.lengths == [RATE, 2 * RATE, 3 * RATE, RATE, RATE]
    partials = [value['text'] for kind, value in events if kind == 'partial']
    assert partials == ['1s of speech', '2s of speech', '', '1s of speech', '']
    finals = [(value['start'], value['text']) for kind, value in events if kind == 'transcript']
    assert finals == [(0.0, '3s of speech'), (3.0, '1s of speech')]
    assert [s['start'] for s in engine.store.segments(mid)] == [0.0, 3.0]


def test_long_speech_without_pause_is_committed_in_bounded_utterances(settings, engine):
    class Phrases:
        def transcribe(self, audio):
            seconds = len(audio) / RATE
            if seconds < 15:
                return [Segment(0, seconds, 'speaking', language='en')]
            return [Segment(0, 10, 'first phrase', language='en'), Segment(10, seconds, 'second phrase', language='en')]
    loud = np.full(RATE, 0.2, np.float32)
    session, mid, events = run_session(settings, engine, [loud] * 16, Phrases())
    finals = [(value['start'], value['text']) for kind, value in events if kind == 'transcript']
    # At 15 s the finished phrase is committed and the unfinished one stays open from 10 s.
    assert finals[0] == (0.0, 'first phrase')
    assert finals[1][0] == 10.0
    assert not session.incomplete


def test_fast_live_model_triggers_accurate_second_pass(settings):
    session = CaptureSession(settings, DesktopSettings(live_model='large-v3-turbo'), lambda *args: None)
    try:
        assert session.two_pass
        assert session.transcriber.settings.model == 'large-v3-turbo'
        assert session.engine.transcriber is not session.transcriber
        assert session.engine.transcriber.settings.model == settings.whisper.model
    finally:
        session.caption_llm.close()


def test_calendar_links_reminders_and_ordering():
    assert calendar.find_join_url('Room 4', 'Join: https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0?context=1>') \
        == 'https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0?context=1'
    assert calendar.find_join_url('https://us02web.zoom.us/j/123?pwd=a.') == 'https://us02web.zoom.us/j/123?pwd=a'
    assert calendar.find_join_url('https://evil.example/teams.microsoft.com/l/meetup-join/x') == ''
    now = datetime(2026, 11, 10, 9, 58)
    soon = calendar.Event(now + timedelta(minutes=2), now + timedelta(minutes=62), 'Coordination', 'https://meet.google.com/abc')
    later = calendar.Event(now + timedelta(hours=3), now + timedelta(hours=4), 'Review')
    ended = calendar.Event(now - timedelta(hours=2), now - timedelta(hours=1), 'Earlier')
    all_day = calendar.Event(now - timedelta(hours=9), now + timedelta(hours=15), 'Leave')
    events = calendar.upcoming([later, ended, all_day, soon], now)
    assert events == [soon, later]
    notified = set()
    assert calendar.due_reminders(events, now, 2, notified) == [soon]
    assert calendar.due_reminders(events, now + timedelta(minutes=1), 2, notified) == []
    assert calendar.describe(soon, now) == 'in 2 min' and calendar.describe(soon, now + timedelta(minutes=5)) == 'in progress'


def test_agenda_and_overdue_use_open_items_only():
    from coordy.desktop.views import draft_agenda, overdue
    base = {'owner': 'P02', 'meeting_date': '2026-11-01', 'status': 'open'}
    late = {**base, 'kind': 'action', 'text': 'Reissue model', 'due': '2020-01-01'}
    vague = {**base, 'kind': 'action', 'text': 'Call client', 'due': 'Thursday'}
    closed = {**base, 'kind': 'action', 'text': 'Old task', 'due': '2020-01-01', 'status': 'closed'}
    question = {**base, 'kind': 'question', 'text': 'Slab thickness?', 'due': ''}
    assert overdue(late) and not overdue(vague) and not overdue(closed)
    agenda = draft_agenda([late, vague, closed, question])
    assert 'Reissue model' in agenda and 'OVERDUE' in agenda and 'Slab thickness?' in agenda
    assert 'Old task' not in agenda and 'Risks to review' in agenda and 'Nothing open' in agenda


def test_due_action_reminders_and_once_per_day(tmp_path):
    from coordy.desktop.startup import ReminderLog, due_summary, reminder_text, command
    today = date(2026, 11, 10)
    def item(text, due, status='open', kind='action'):
        return {'kind': kind, 'text': text, 'due': due, 'status': status}
    overdue, due_today = due_summary([item('Late', '2026-11-01'), item('Today', '2026-11-10'), item('Soon', '2026-11-20'),
                                      item('Done', '2026-11-01', 'closed'), item('Vague', 'Thursday'),
                                      item('Risk', '2026-11-01', kind='risk')], today)
    assert [i['text'] for i in overdue] == ['Late'] and [i['text'] for i in due_today] == ['Today']
    assert reminder_text(overdue, due_today).startswith('1 overdue and 1 due today. First: Late')
    assert reminder_text([], []) == ''
    log = ReminderLog(tmp_path)
    assert log.due(today)
    log.mark(today)
    assert not log.due(today) and log.due(date(2026, 11, 11))
    assert '-m coordy.desktop.app --background' in command()


pytest.importorskip('PySide6')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def make_meeting(engine, mid='m1'):
    engine.store.upsert_meeting(mid, title='Weekly coordination', date=date.today().isoformat(), status='done', duration=600)
    engine.store.replace_segments(mid, [{'start': 0, 'end': 2, 'speaker': 'S1', 'language': 'en', 'text': 'Freeze the facade module'}])
    engine.write_transcript(mid)
    engine.store.replace_llm_items(mid, [{'kind': 'action', 'text': 'Issue the BOQ', 'owner': 'P01', 'due': '2020-01-01'}])
    engine.store.upsert_meeting(mid, minutes=json.dumps({'summary': 'Facade frozen.'}))
    engine.write_minutes(mid)
    (engine.settings.paths.raw / f'{mid}.json').write_text('{"segments": []}', encoding='utf-8')


def test_detected_meeting_asks_before_recording_and_library_is_searchable(settings, engine):
    from coordy.desktop.app import Window
    from coordy.desktop.detection import Activity
    app = QApplication.instance() or QApplication([])
    make_meeting(engine)
    engine.store.upsert_meeting('m2', title='Failed import', date='2026-01-01', status='error', error='Ollama down')
    window = Window()
    window.show()
    assert window.stats['meetings'].text() == '1' and window.stats['late'].text() == '1' and window.stats['failed'].text() == '1'
    started = []
    window.start_capture = lambda *args: started.append(True)
    activity = [Activity('ms-teams', 'Weekly sync')]
    window.detected(activity)
    window.detected(activity)
    # Default is to ask: a banner appears and nothing records until the user agrees.
    assert window.banner.isVisible() and 'Teams meeting detected' in window.banner_text.text()
    assert not started
    window.banner_accept()
    assert started and window.detection_owns_capture
    assert window.history.count() == 2
    assert 'Minutes ready' in window.history.item(0).text() and 'Needs attention' in window.history.item(1).text()
    window.search.setText('facade')  # matches the transcript, not the title
    assert window.history.count() == 1
    window.search.clear()
    window.history.setCurrentRow(0)
    assert window.send.isEnabled() and window.title.text() == 'Weekly coordination'
    assert 'Facade frozen.' in window.minutes.toPlainText()
    window.quitting = True
    window.close()


def test_export_pdf_delete_meeting_and_action_register(settings, engine, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from coordy.desktop.app import Window
    app = QApplication.instance() or QApplication([])
    make_meeting(engine)
    window = Window()
    window.load_meeting('m1')
    target = tmp_path / 'minutes.pdf'
    window.export_minutes('pdf', str(target))
    assert target.read_bytes().startswith(b'%PDF') and target.stat().st_size > 1000
    page = window.actions_page
    window.go(2)
    assert page.table.rowCount() == 1 and 'overdue' in page.table.item(0, 3).text()
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert engine.store.items(meeting_id='m1')[0]['status'] == 'closed'
    assert page.table.rowCount() == 0 and window.stats['open'].text() == '0'
    monkeypatch.setattr(QMessageBox, 'question', lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    window.delete_meeting()
    assert engine.store.meeting('m1') is None and window.history.count() == 0
    assert not (settings.paths.minutes / 'm1.md').exists()
    assert not window.send.isEnabled()
    window.quitting = True
    window.close()
