"""Small shared interface pieces: themed line icons, cards and status pills."""
import re

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTextDocument
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QVBoxLayout

# 24x24 stroke paths; "fill:" entries are drawn solid.
ICONS = {
    "home": "M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
    "meetings": "M4 6h16M4 12h16M4 18h10",
    "actions": "M8.5 12l2.5 2.5 4.5-5M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18z",
    "settings": "M4 7h9M19 7h1M4 17h1M11 17h9M16 4.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 0 0 0-5zM8 14.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 0 0 0-5z",
    "folder": "M3 7a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z",
    "import": "M12 16V4M7 9l5-5 5 5M4 20h16",
    "calendar": "M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zM4 10h16M8 3v4M16 3v4",
    "guest": "M9 11a4 4 0 1 0 0-8a4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M19 8v6M16 11h6",
    "record": "fill:M12 5a7 7 0 1 0 0 14a7 7 0 0 0 0-14z",
    "stop": "fill:M7 6h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z",
}


def color(token):
    return (QApplication.instance().property("coordColors") or {}).get(token, "#646B7A")


def icon(name, token="muted", size=20):
    path = ICONS[name]
    tint = token if token.startswith("#") else color(token)
    style = (f'fill="{tint}" stroke="none"' if path.startswith("fill:") else
             f'fill="none" stroke="{tint}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"')
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{path.removeprefix("fill:")}" {style}/></svg>'
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def dot(token, size=10):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color(token)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, size * 2 - 4, size * 2 - 4)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def button(text, action, kind="", icon_name="", tip=""):
    item = QPushButton(text)
    item.setObjectName(kind)
    item.clicked.connect(action)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        item.setProperty("iconName", icon_name)
        item.setIconSize(QSize(18, 18))
    if tip:
        item.setToolTip(tip)
    return item


def label(text, kind="", wrap=False):
    item = QLabel(text)
    item.setObjectName(kind)
    item.setWordWrap(wrap)
    return item


def set_state(widget, state, text=None):
    """Restyle a pill for its state (idle, rec, busy, ok, warn, error)."""
    widget.setProperty("state", state)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    if text is not None:
        widget.setText(text)


class Card(QFrame):
    def __init__(self, title="", hint=""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        if title:
            self.body.addWidget(label(title, "cardTitle"))
        if hint:
            self.body.addWidget(label(hint, "muted", wrap=True))


def document_html(markdown, colors=None):
    """Markdown minutes as tidy rich text: spaced headings and clean full-width tables."""
    colors = colors or (QApplication.instance().property("coordColors") or {})
    border, shade, text = colors.get("border", "#E3E5EA"), colors.get("sunken", "#F0F1F4"), colors.get("text", "#16181D")
    source = QTextDocument()
    source.setMarkdown(markdown)
    page = source.toHtml()
    page = re.sub(r"<body[^>]*>", f'<body style="color:{text};">', page)
    page = page.replace('<h1 style=" margin-top:0px; margin-bottom:0px;', '<h1 style=" margin-top:0px; margin-bottom:12px;')
    page = page.replace('<h2 style=" margin-top:0px; margin-bottom:0px;', '<h2 style=" margin-top:20px; margin-bottom:8px;')
    page = page.replace("font-size:xx-large", "font-size:17pt").replace("font-size:x-large", "font-size:12.5pt")
    page = re.sub(r"<table[^>]*>\s*<tr>", f'<table width="100%" border="1" cellspacing="0" cellpadding="7" '
                  f'style="border-collapse:collapse; border-color:{border};"><tr bgcolor="{shade}">', page)
    return page
