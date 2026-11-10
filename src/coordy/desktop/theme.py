"""APG application palettes, including dialogs and native-style Qt controls."""
from pathlib import Path
from string import Template

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).parent / "assets"

LIGHT = dict(
    page="#F6F7F9", surface="#FFFFFF", sunken="#F0F1F4", text="#16181D",
    muted="#646B7A", disabled="#737B89", border="#E3E5EA", strong="#D6D9E0",
    accent="#1F2DA8", accent_hover="#172284", accent_text="#FFFFFF",
    link="#1F2DA8", tint="#EEF0FB", tint_border="#DDE1F7",
    danger="#B3261E", danger_bg="#FCEBEA", danger_border="#F3C9C5",
    ok="#1B7F4B", ok_bg="#E6F4EC", warn="#8A5A00", warn_bg="#FFF4D6", rec="#D92D20",
)
DARK = dict(
    page="#16181D", surface="#20242C", sunken="#191C23", text="#F1F3F7",
    muted="#AFB7C7", disabled="#8791A4", border="#394151", strong="#535E73",
    accent="#1F2DA8", accent_hover="#3646C8", accent_text="#FFFFFF",
    link="#B8C5FF", tint="#292F50", tint_border="#505F91",
    danger="#FFBCB6", danger_bg="#482A2E", danger_border="#855354",
    ok="#7EE0A8", ok_bg="#1E3A2B", warn="#FFD479", warn_bg="#3D3319", rec="#D92D20",
)


def colors_for(scheme) -> dict:
    return DARK if scheme == Qt.ColorScheme.Dark else LIGHT


def apply_theme(app: QApplication, scheme: Qt.ColorScheme) -> None:
    colors = colors_for(scheme)
    app.setProperty("coordColors", colors)
    # Palette roles also cover controls/popups that do not use the stylesheet.
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "page", QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "surface", QPalette.ColorRole.AlternateBase: "sunken",
        QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "surface",
        QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.ToolTipBase: "surface",
        QPalette.ColorRole.ToolTipText: "text", QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.HighlightedText: "accent_text", QPalette.ColorRole.Link: "link",
        QPalette.ColorRole.LinkVisited: "link", QPalette.ColorRole.PlaceholderText: "muted",
        QPalette.ColorRole.Light: "strong", QPalette.ColorRole.Midlight: "border",
        QPalette.ColorRole.Mid: "border", QPalette.ColorRole.Dark: "sunken",
        QPalette.ColorRole.Shadow: "page", QPalette.ColorRole.BrightText: "accent_text",
    }
    for role, token in roles.items():
        palette.setColor(role, QColor(colors[token]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled"]))
    app.setPalette(palette)
    stylesheet = Template((ASSETS / "apg.qss").read_text(encoding="utf-8"))
    mode = "dark" if scheme == Qt.ColorScheme.Dark else "light"
    icons = {name: (ASSETS / f"{name}-{mode}.svg").as_posix() for name in ("up", "down")}
    icons["check"] = (ASSETS / "check.svg").as_posix()
    app.setStyleSheet(stylesheet.substitute(colors, **icons))


class ThemeManager(QObject):
    """Follow Windows/Qt appearance, including changes while settings is open."""
    def __init__(self, app: QApplication):
        super().__init__(app)
        # Fusion paints checkboxes/spin arrows using our palette instead of mixing
        # native Windows dark surfaces with application-defined light controls.
        app.setStyle("Fusion")
        font = QFont("IBM Plex Sans")
        font.setPointSizeF(10.5)
        app.setFont(font)
        self.app = app
        app.styleHints().colorSchemeChanged.connect(self.refresh)
        self.refresh(app.styleHints().colorScheme())

    def refresh(self, scheme):
        apply_theme(self.app, scheme)
