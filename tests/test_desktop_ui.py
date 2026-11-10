import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import QPoint
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from coordy.desktop.app import ASSETS, Window


def test_window_state_and_transcript_rendering(settings, engine):
    app = QApplication.instance() or QApplication([])
    window = Window()
    window.show()
    app.processEvents()
    assert not window.stop.isEnabled()
    assert not window.send.isEnabled()
    window.on_event('transcript', {'start': 12, 'end': 15, 'language': 'ar', 'text': 'مرحبا <script>unsafe</script>'})
    assert 'مرحبا <script>unsafe</script>' in window.transcript.toPlainText()
    window.set_busy(True)
    assert not window.import_button.isEnabled()
    assert not window.models.isEnabled()
    window.set_busy(False)
    assert window.import_button.isEnabled()
    window.close()


@pytest.mark.parametrize('dark', [False, True])
def test_settings_readable_and_operable_in_both_themes(settings, dark, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPalette
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel
    from coordy.desktop.app import Preferences
    from coordy.desktop.theme import apply_theme, ThemeManager

    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(app)
    scheme = Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light
    apply_theme(app, scheme)
    window = Window()
    dialog = Preferences(window)
    dialog.show()
    app.processEvents()

    def luminance(color):
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, [0.2126, 0.7152, 0.0722]))

    def contrast(a, b):
        light, dark = sorted([luminance(a), luminance(b)], reverse=True)
        return (light + 0.05) / (dark + 0.05)

    # Check the rendered dialog surface, not just the configured palette.
    background = dialog.grab().toImage().pixelColor(5, 5)
    assert (background.lightness() < 128) == dark
    label = next(label for label in dialog.findChildren(QLabel) if label.text() == 'Live translation')
    assert contrast(label.palette().color(QPalette.ColorRole.WindowText), background) >= 4.5
    for key in ['smtp_host', 'smtp_port', 'translation']:
        control = dialog.controls[key]
        palette = control.palette()
        assert contrast(palette.color(QPalette.ColorRole.Text), palette.color(QPalette.ColorRole.Base)) >= 4.5
        assert control.font().pointSizeF() > 0

    combo = dialog.controls['translation']
    dialog.select('translation')
    app.processEvents()
    combo.showPopup()
    app.processEvents()
    popup_palette = combo.view().palette()
    assert contrast(popup_palette.color(QPalette.ColorRole.Text), popup_palette.color(QPalette.ColorRole.Base)) >= 4.5
    combo.hidePopup()
    dialog.select('smtp_host')
    app.processEvents()
    dialog.controls['smtp_host'].setFocus()
    QTest.keyClicks(dialog.controls['smtp_host'], 'smtp.example.com')
    checkbox = dialog.controls['auto_send']
    QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton, pos=checkbox.rect().topLeft() + QPoint(8, checkbox.height() // 2))
    actions = dialog.findChild(QDialogButtonBox)
    QTest.mouseClick(actions.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert window.preferences.smtp_host == 'smtp.example.com'
    assert window.preferences.auto_send
    original = QPixmap(str(ASSETS / 'apg-logo.png'))
    displayed = window.logo.pixmap()
    assert displayed.width() / displayed.height() == pytest.approx(original.width() / original.height(), abs=0.05)
    window.close()
    manager.deleteLater()
    apply_theme(app, Qt.ColorScheme.Light)


def test_open_settings_updates_when_system_theme_changes(settings):
    from PySide6.QtCore import Qt
    from coordy.desktop.app import Preferences
    from coordy.desktop.theme import ThemeManager
    app = QApplication.instance() or QApplication([])
    manager = ThemeManager(app)
    manager.refresh(Qt.ColorScheme.Light)
    window = Window()
    dialog = Preferences(window)
    dialog.show()
    app.processEvents()
    light = dialog.grab().toImage().pixelColor(5, 5)
    # Same slot used by QStyleHints.colorSchemeChanged on Windows.
    manager.refresh(Qt.ColorScheme.Dark)
    app.processEvents()
    dark = dialog.grab().toImage().pixelColor(5, 5)
    assert light.lightness() > 200 and dark.lightness() < 60
    assert not window.send.isEnabled()
    assert not window.stop.isEnabled()
    dialog.reject()
    window.close()
    manager.refresh(Qt.ColorScheme.Light)
    manager.deleteLater()


def test_live_caption_language_can_change_during_capture(settings):
    from unittest.mock import Mock
    from coordy.desktop.settings import DesktopSettings
    app = QApplication.instance() or QApplication([])
    window = Window()
    window.show()
    window.session = Mock()
    window.set_busy(True)
    assert window.captions.target.isEnabled()
    window.captions.target.setCurrentIndex(window.captions.target.findData('Arabic'))
    assert DesktopSettings.load(settings.paths.root).translation == 'Arabic'
    window.session.set_translation.assert_called_once_with('Arabic')
    source = {'start': 12, 'end': 15, 'language': 'en', 'text': 'P01 approved <design>.'}
    window.on_event('transcript', source)
    assert window.captions.original.toPlainText() == source['text']
    window.on_event('translation', {**source, 'language': 'Arabic', 'text': 'وافق P01 على التصميم.'})
    assert 'وافق P01' in window.captions.translated.toPlainText()
    app.processEvents()
    assert window.captions.translated.document().size().height() <= window.captions.translated.viewport().height()
    window.captions.target.setCurrentIndex(window.captions.target.findData('English'))
    window.on_event('translation', {**source, 'language': 'Arabic', 'text': 'Old reply'})
    assert not window.captions.translated.toPlainText()
    window.on_event('transcript', {**source, 'start': 20, 'end': 25})
    window.on_event('translation', {**source, 'language': 'English', 'text': 'Earlier segment'})
    assert window.captions.translated.toPlainText() == 'Earlier segment'
    assert '00:12' in window.captions.translation_status.text()
    window.on_event('translation', {**source, 'start': 1, 'language': 'English', 'text': 'Out of order'})
    assert window.captions.translated.toPlainText() == 'Earlier segment'
    window.on_event('caption_translation_error', {'start': 20, 'end': 25, 'language': 'English'})
    assert 'recording and original captions continue' in window.captions.translated.toPlainText()
    window.captions.target.setCurrentIndex(0)
    assert window.captions.translated.isHidden()
    assert window.captions.original.toPlainText() == source['text']
    window.session = None
    window.set_busy(False)
    window.close()


def test_audio_device_selection_persists_and_source_meters_are_independent(settings):
    from coordy.desktop.app import Preferences
    app = QApplication.instance() or QApplication([])
    window = Window()
    dialog = Preferences(window)
    dialog.populate_devices({'speakers': [{'name': 'Meeting headset [Loopback]'}],
                             'microphones': [{'name': 'USB microphone'}]})
    dialog.controls['speaker_device'].setCurrentIndex(1)
    dialog.controls['microphone_device'].setCurrentIndex(1)
    dialog.accept()
    assert window.preferences.speaker_device == 'Meeting headset [Loopback]'
    assert window.preferences.microphone_device == 'USB microphone'
    dialog = Preferences(window)
    dialog.populate_devices({'speakers': [], 'microphones': []})
    assert dialog.controls['speaker_device'].currentData() == 'Meeting headset [Loopback]'
    assert 'Unavailable' in dialog.controls['speaker_device'].currentText()
    dialog.reject()
    window.on_event('audio_devices', {'computer': 'Meeting headset', 'microphone': 'USB mic'})
    assert 'Meeting headset' in window.computer_level.toolTip()
    window.on_event('audio_levels', {'computer': 0, 'microphone': 80, 'computer_silent_seconds': 10})
    assert window.computer_level.value() == 0 and window.mic_level.value() == 80
    assert 'No computer sound' in window.statusBar().currentMessage()
    window.on_event('audio_levels', {'computer': 60, 'microphone': 0, 'computer_silent_seconds': 0})
    assert window.computer_level.value() == 60
    assert window.statusBar().currentMessage() == 'Computer sound received.'
    window.close()


def test_caption_processing_status_and_saved_meeting_error_are_visible(settings, engine):
    app = QApplication.instance() or QApplication([])
    window = Window()
    window.on_event('speech_state', {'message': 'Loading Whisper on CPU', 'busy': True})
    assert 'Loading Whisper on CPU' in window.captions.notice.text()
    assert 'elapsed' in window.captions.notice.text()
    window.on_event('speech_state', {'message': 'No speech recognized', 'busy': False})
    assert window.captions.notice.text() == 'No speech recognized'
    engine.store.upsert_meeting('failed-meeting', status='error', error='CUDA unavailable <details>')
    window.load_meeting('failed-meeting')
    assert 'CUDA unavailable <details>' in window.minutes.toPlainText()
    window.close()
