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
    window.on_event('transcript', {'start': 12, 'language': 'ar', 'text': 'مرحبا <script>unsafe</script>'})
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
    combo.showPopup()
    app.processEvents()
    popup_palette = combo.view().palette()
    assert contrast(popup_palette.color(QPalette.ColorRole.Text), popup_palette.color(QPalette.ColorRole.Base)) >= 4.5
    combo.hidePopup()
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
