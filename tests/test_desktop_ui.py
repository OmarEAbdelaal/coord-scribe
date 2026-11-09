import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from coordy.desktop.app import Window


def test_window_state_and_transcript_rendering(settings, engine):
    app = QApplication.instance() or QApplication([])
    window = Window()
    window.show()
    app.processEvents()
    assert not window.stop.isEnabled()
    assert not window.send.isEnabled()
    window.on_event('transcript', {'start': 12, 'language': 'ar', 'text': 'مرحبا <script>unsafe</script>'})
    assert 'مرحبا <script>unsafe</script>' in window.transcript.toPlainText()
    window.set_busy(True)
    assert not window.import_button.isEnabled()
    assert not window.models.isEnabled()
    window.set_busy(False)
    assert window.import_button.isEnabled()
    window.close()
