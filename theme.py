"""Colours, stylesheet, icon and the custom toggle switch."""
from __future__ import annotations

from PySide6.QtCore import Property, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QCheckBox

BG = "#0a0c12"
CARD = "#12151e"
CARD_HI = "#1a1e2b"
BORDER = "#232838"
TEXT = "#eceef5"
MUTED = "#8f97ad"
ACCENT = "#7c6cff"
ACCENT2 = "#4f8cff"
DANGER = "#ff5470"
OK = "#35d6a0"
WARN = "#ffb84d"

QSS = """
* { font-family: 'Segoe UI Variable Display', 'Segoe UI Variable Text', 'Segoe UI', 'Inter', sans-serif; font-size: 13px; color: %(TEXT)s; }
QMainWindow, QWidget#root, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget { background: %(BG)s; }
QWidget#page { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #101320, stop:0.45 %(BG)s, stop:1 #07080c); }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QLabel { background: transparent; }
QLabel#title { font-size: 20px; font-weight: 800; letter-spacing: 0.4px; }
QLabel#ver { color: %(MUTED)s; font-size: 10.5px; font-weight: 700; letter-spacing: 0.8px; background: %(CARD_HI)s;
             border: 1px solid %(BORDER)s; border-radius: 8px; padding: 2px 7px; }
QLabel#pageTitle { font-size: 18px; font-weight: 800; }
QLabel#cardTitle { color: %(MUTED)s; font-size: 10.5px; font-weight: 800; letter-spacing: 1.8px; }
QLabel#sub { color: %(MUTED)s; font-size: 11.5px; }
QLabel#hint { color: %(MUTED)s; font-size: 12px; }
QLabel#note { font-size: 12px; }
QLabel#big { font-size: 34px; font-weight: 800; letter-spacing: 0.5px; }
QLabel#footer { color: #5b6277; font-size: 11px; }
QFrame#card { background: %(CARD)s; border: 1px solid %(BORDER)s; border-radius: 18px; }
QFrame#hero { border-radius: 22px; border: 1px solid #34305f;
              background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1d1a3d, stop:0.55 #14172a, stop:1 #101420); }
QFrame#sep { background: %(BORDER)s; max-height: 1px; min-height: 1px; border: none; }

QPushButton { background: %(CARD_HI)s; border: 1px solid %(BORDER)s; border-radius: 11px; padding: 8px 15px; }
QPushButton:hover { border-color: #3c4460; background: #212638; }
QPushButton:pressed { background: #151926; }
QPushButton:disabled { color: #5d6479; }
QPushButton#ghost { background: transparent; border: 1px solid %(BORDER)s; color: %(MUTED)s; }
QPushButton#ghost:hover { color: %(TEXT)s; border-color: #3c4460; background: %(CARD)s; }
QPushButton#icon { background: %(CARD)s; border: 1px solid %(BORDER)s; border-radius: 13px; padding: 0; font-size: 17px; }
QPushButton#icon:hover { background: %(CARD_HI)s; border-color: #3c4460; }
QPushButton#save {
    border: 1px solid rgba(255,255,255,0.18); border-radius: 20px; font-size: 19px; font-weight: 800; padding: 0 22px; letter-spacing: 0.3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %(ACCENT)s, stop:1 %(ACCENT2)s);
}
QPushButton#save:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9186ff, stop:1 #669cff); }
QPushButton#save:pressed { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6a5be8, stop:1 #4278e6); }
QPushButton#save:disabled { background: #232837; color: #6f768c; border: 1px solid %(BORDER)s; }
QPushButton#chip { background: %(CARD_HI)s; border: 1px solid %(BORDER)s; border-radius: 999px; padding: 8px 0; min-width: 54px; color: %(MUTED)s; font-weight: 600; }
QPushButton#chip:hover { color: %(TEXT)s; border-color: #3c4460; }
QPushButton#chip:checked { background: rgba(124,108,255,0.20); border-color: %(ACCENT)s; color: #ffffff; font-weight: 800; }

QLabel#pill { border-radius: 12px; padding: 5px 13px; font-size: 12px; font-weight: 700; background: %(CARD_HI)s; color: %(MUTED)s; border: 1px solid %(BORDER)s; }
QLabel#pill[state="recording"] { background: rgba(53,214,160,0.12); color: %(OK)s; border-color: rgba(53,214,160,0.35); }
QLabel#pill[state="starting"] { background: rgba(255,184,77,0.12); color: %(WARN)s; border-color: rgba(255,184,77,0.35); }
QLabel#pill[state="error"] { background: rgba(255,84,112,0.12); color: %(DANGER)s; border-color: rgba(255,84,112,0.35); }

QComboBox, QSpinBox, QLineEdit, QKeySequenceEdit {
    background: %(CARD_HI)s; border: 1px solid %(BORDER)s; border-radius: 10px; padding: 7px 11px; min-height: 20px;
    selection-background-color: %(ACCENT)s;
}
QComboBox:hover, QSpinBox:hover, QLineEdit:hover { border-color: #3c4460; }
QComboBox:focus, QSpinBox:focus, QLineEdit:focus, QKeySequenceEdit:focus { border-color: %(ACCENT)s; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: %(CARD_HI)s; border: 1px solid %(BORDER)s; selection-background-color: %(ACCENT)s; outline: none; padding: 4px; }
QComboBox:disabled, QSpinBox:disabled, QLineEdit:disabled { color: #5d6479; }

QSlider::groove:horizontal { height: 5px; background: %(BORDER)s; border-radius: 2px; }
QSlider::sub-page:horizontal { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %(ACCENT)s, stop:1 %(ACCENT2)s); border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 15px; height: 15px; margin: -5px 0; border-radius: 7px; }

QProgressBar { background: %(CARD_HI)s; border: none; border-radius: 5px; max-height: 10px; min-height: 10px; text-align: center; }
QProgressBar::chunk { border-radius: 5px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %(ACCENT)s, stop:1 %(ACCENT2)s); }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2c3243; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #3c4460; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0; background: none; }
QToolTip { background: %(CARD_HI)s; color: %(TEXT)s; border: 1px solid %(BORDER)s; padding: 6px; }
QMenu { background: %(CARD)s; border: 1px solid %(BORDER)s; padding: 6px; border-radius: 10px; }
QMenu::item { padding: 8px 24px; border-radius: 7px; }
QMenu::item:selected { background: %(ACCENT)s; }
QMenu::separator { height: 1px; background: %(BORDER)s; margin: 5px 8px; }
QMessageBox, QProgressDialog, QDialog { background: %(CARD)s; }
""" % dict(BG=BG, CARD=CARD, CARD_HI=CARD_HI, BORDER=BORDER, TEXT=TEXT, MUTED=MUTED, ACCENT=ACCENT,
           ACCENT2=ACCENT2, DANGER=DANGER, OK=OK, WARN=WARN)


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(CARD_HI))
    pal.setColor(QPalette.AlternateBase, QColor(CARD))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(CARD_HI))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(CARD_HI))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#5d6479"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5d6479"))
    app.setPalette(pal)
    app.setStyleSheet(QSS)


def make_pixmap(size: int = 256) -> QPixmap:
    import base64
    from icon_data import ICON_PNG_B64
    pm = QPixmap()
    pm.loadFromData(base64.b64decode(ICON_PNG_B64), "PNG")
    if size != pm.width():
        pm = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pm


def make_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_pixmap(s))
    return icon


class Toggle(QCheckBox):
    """iOS-style switch."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._offset = 1.0 if checked else 0.0
        self.setFixedSize(46, 26)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self.setChecked(checked)
        self.toggled.connect(self._animate)

    def _get(self) -> float:
        return self._offset

    def _set(self, v: float) -> None:
        self._offset = v
        self.update()

    offset = Property(float, _get, _set)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self) -> QSize:
        return QSize(46, 26)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor("#2b3042"), QColor(ACCENT)
        t = self._offset
        col = QColor(int(off.red() + (on.red() - off.red()) * t),
                     int(off.green() + (on.green() - off.green()) * t),
                     int(off.blue() + (on.blue() - off.blue()) * t))
        if not self.isEnabled():
            col.setAlpha(110)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0, 0, 46, 26), 13, 13)
        p.setBrush(QColor("#ffffff"))
        x = 3 + 20 * t
        p.drawEllipse(QRectF(x, 3, 20, 20))
