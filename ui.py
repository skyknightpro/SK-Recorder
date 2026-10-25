"""Main window and system-tray integration."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QKeySequenceEdit,
    QLabel, QLineEdit, QMainWindow, QMenu, QProgressBar, QPushButton, QScrollArea, QSlider,
    QSpinBox, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget,
)

import audio
import autostart
import config
import engine as E
import theme
from config import APP_NAME, APP_VERSION, ENCODER_LABELS, FPS_OPTIONS, RESOLUTIONS, Settings
from hotkeys import HotkeyManager
from theme import Toggle


class Bridge(QObject):
    """Moves callbacks from worker threads onto the Qt main thread."""
    state = Signal(str, str)
    saved = Signal(str, float)
    failed = Signal(str)
    hotkey = Signal()


def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def fmt_len(sec: int) -> str:
    if sec < 60:
        return f"{sec} sec"
    m, s = divmod(sec, 60)
    return f"{m} min" if s == 0 else f"{m} min {s} sec"


def reveal_in_folder(path: str) -> None:
    p = Path(path)
    if sys.platform == "win32" and p.exists():
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent if p.is_file() else p)))


def make_card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(12)
    if title:
        t = QLabel(title.upper())
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return f, lay


def make_row(label: str, widget: QWidget, sub: str = "") -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(12)
    left = QVBoxLayout()
    left.setSpacing(1)
    left.addWidget(QLabel(label))
    if sub:
        s = QLabel(sub)
        s.setObjectName("sub")
        s.setWordWrap(True)
        left.addWidget(s)
    h.addLayout(left, 1)
    h.addWidget(widget, 0, Qt.AlignRight | Qt.AlignVCenter)
    return w


def separator() -> QFrame:
    f = QFrame()
    f.setObjectName("sep")
    f.setFrameShape(QFrame.NoFrame)
    return f


class MainWindow(QMainWindow):
    def __init__(self, ffmpeg: str, settings: Settings, start_hidden: bool = False):
        super().__init__()
        self.s = settings
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION[:3]}")
        self.setWindowIcon(theme.make_icon())
        self.setMinimumSize(460, 720)
        self.resize(460, 760)
        self._quitting = False
        self._tray_hint_shown = False
        self._last_clip = ""
        self._msg_kind = "hint"

        self.bridge = Bridge()
        self.bridge.state.connect(self._on_state)
        self.bridge.saved.connect(self._on_saved)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.hotkey.connect(self.do_save)

        self.engine = E.ReplayEngine(
            ffmpeg, self.s,
            on_state=self.bridge.state.emit,
            on_saved=self.bridge.saved.emit,
            on_failed=self.bridge.failed.emit,
        )
        self.hotkeys = HotkeyManager(self.bridge.hotkey.emit)

        self._restart_timer = QTimer(self, singleShot=True, interval=900)
        self._restart_timer.timeout.connect(self._apply_capture_changes)
        self._tick = QTimer(self, interval=1000)
        self._tick.timeout.connect(self._refresh_buffer)
        self._tick.start()
        self._busy_timer = QTimer(self, singleShot=True, interval=600)  # keeps "Saving..." visible briefly

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.stack.addWidget(self._build_home())
        self.stack.addWidget(self._build_settings())

        self._build_tray()
        self._refresh_all()
        if not self.hotkeys.set_hotkey(self.s.hotkey):
            self._set_message(f"Hotkey not active: {self.hotkeys.error}", "error")

        # encoder availability arrives asynchronously after probing ffmpeg
        self._probe_timer = QTimer(self, interval=400)
        self._probe_timer.timeout.connect(self._check_probe)
        self._probe_timer.start()

        if self.s.auto_start_buffer:
            self.engine.start()
        else:
            self._on_state(E.STOPPED, "")
        if not start_hidden:
            self.show()

    # ================================================================= HOME PAGE
    def _build_home(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)

        # header
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(theme.make_pixmap(72).scaled(38, 38, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(logo)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        head.addWidget(title)
        ver = QLabel(f"v{APP_VERSION[:3]}")
        ver.setObjectName("ver")
        head.addWidget(ver, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.pill = QLabel("Stopped")
        self.pill.setObjectName("pill")
        head.addWidget(self.pill)
        gear = QPushButton("⚙")
        gear.setObjectName("icon")
        gear.setFixedSize(38, 38)
        gear.setCursor(Qt.PointingHandCursor)
        gear.setToolTip("Settings")
        gear.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        head.addWidget(gear)
        lay.addLayout(head)

        # hero
        hero, hl = make_card()
        hero.setObjectName("hero")
        hl.setContentsMargins(20, 20, 20, 16)
        hl.setSpacing(14)
        self.save_btn = QPushButton("Save Clip")
        self.save_btn.setObjectName("save")
        self.save_btn.setFixedHeight(84)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.do_save)
        hl.addWidget(self.save_btn)
        self.hotkey_label = QLabel()
        self.hotkey_label.setObjectName("hint")
        self.hotkey_label.setAlignment(Qt.AlignCenter)
        hl.addWidget(self.hotkey_label)
        lay.addWidget(hero)

        # clip length
        card, cl = make_card("Clip length")
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.chip_values = [30, 60, 120, 300, 0]  # 0 = custom
        for i, (txt, val) in enumerate(zip(["30 s", "1 min", "2 min", "5 min", "Custom"], self.chip_values)):
            b = QPushButton(txt)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.chip_group.addButton(b, i)
            chips.addWidget(b)
        cl.addLayout(chips)
        self.chip_group.idClicked.connect(self._chip_clicked)

        self.custom_row = QWidget()
        cr = QHBoxLayout(self.custom_row)
        cr.setContentsMargins(0, 0, 0, 0)
        self.custom_min = QSpinBox()
        self.custom_min.setSuffix(" min")
        self.custom_sec = QSpinBox()
        self.custom_sec.setSuffix(" sec")
        self.custom_sec.setRange(0, 59)
        cr.addWidget(QLabel("Save the last"))
        cr.addStretch(1)
        cr.addWidget(self.custom_min)
        cr.addWidget(self.custom_sec)
        self.custom_min.valueChanged.connect(self._custom_changed)
        self.custom_sec.valueChanged.connect(self._custom_changed)
        cl.addWidget(self.custom_row)
        lay.addWidget(card)

        # buffer
        card, bl = make_card("Replay buffer")
        top = QHBoxLayout()
        self.buffer_text = QLabel("Off")
        self.buffer_text.setObjectName("big")
        top.addWidget(self.buffer_text)
        top.addStretch(1)
        self.master_toggle = Toggle(False)
        self.master_toggle.setToolTip("Turn the always-on replay buffer on or off")
        self.master_toggle.clicked.connect(self._master_clicked)
        top.addWidget(self.master_toggle, 0, Qt.AlignVCenter)
        bl.addLayout(top)
        self.buffer_bar = QProgressBar()
        self.buffer_bar.setTextVisible(False)
        bl.addWidget(self.buffer_bar)
        self.buffer_sub = QLabel("")
        self.buffer_sub.setObjectName("sub")
        bl.addWidget(self.buffer_sub)
        lay.addWidget(card)

        # message line
        self.note = QLabel("")
        self.note.setObjectName("note")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        lay.addStretch(1)

        # footer
        foot = QHBoxLayout()
        open_btn = QPushButton("Open clips folder")
        open_btn.setObjectName("ghost")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self.open_folder)
        foot.addWidget(open_btn)
        self.reveal_btn = QPushButton("Show last clip")
        self.reveal_btn.setObjectName("ghost")
        self.reveal_btn.setCursor(Qt.PointingHandCursor)
        self.reveal_btn.clicked.connect(lambda: reveal_in_folder(self._last_clip))
        self.reveal_btn.hide()
        foot.addWidget(self.reveal_btn)
        foot.addStretch(1)
        fv = QLabel(f"SK Recorder \u00b7 v{APP_VERSION}")
        fv.setObjectName("footer")
        foot.addWidget(fv)
        lay.addLayout(foot)
        return page

    # ================================================================= SETTINGS PAGE
    def _build_settings(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(22, 20, 22, 14)
        outer.setSpacing(12)

        head = QHBoxLayout()
        back = QPushButton("←")
        back.setObjectName("icon")
        back.setFixedSize(38, 38)
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        head.addWidget(back)
        t = QLabel("Settings")
        t.setObjectName("pageTitle")
        head.addWidget(t)
        head.addStretch(1)
        outer.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ---- Video
        card, cl = make_card("Video")
        self.monitor_combo = QComboBox()
        for i, scr in enumerate(QGuiApplication.screens()):
            sz = scr.size() * scr.devicePixelRatio()
            self.monitor_combo.addItem(f"Display {i + 1}  ·  {int(sz.width())}×{int(sz.height())}", i)
        self._select_data(self.monitor_combo, self.s.monitor)
        self.monitor_combo.currentIndexChanged.connect(lambda i: self.set("monitor", self.monitor_combo.itemData(i)))
        cl.addWidget(make_row("Screen", self.monitor_combo, "Full-screen capture of the chosen display"))

        self.fps_combo = QComboBox()
        for f in FPS_OPTIONS:
            self.fps_combo.addItem(f"{f} fps", f)
        self._select_data(self.fps_combo, self.s.fps)
        self.fps_combo.currentIndexChanged.connect(lambda i: self.set("fps", self.fps_combo.itemData(i)))
        cl.addWidget(make_row("Frame rate", self.fps_combo))

        self.quality_combo = QComboBox()
        for q, kb in config.QUALITY_KBPS.items():
            self.quality_combo.addItem(f"{q}  ({kb // 1000} Mbps)", q)
        self._select_data(self.quality_combo, self.s.quality)
        self.quality_combo.currentIndexChanged.connect(lambda i: self.set("quality", self.quality_combo.itemData(i)))
        cl.addWidget(make_row("Quality", self.quality_combo))

        self.res_combo = QComboBox()
        for name, h in RESOLUTIONS:
            self.res_combo.addItem(name, h)
        self._select_data(self.res_combo, self.s.resolution)
        self.res_combo.currentIndexChanged.connect(lambda i: self.set("resolution", self.res_combo.itemData(i)))
        cl.addWidget(make_row("Resolution", self.res_combo, "Downscale only - never upscales"))

        self.enc_combo = QComboBox()
        for k, label in ENCODER_LABELS.items():
            self.enc_combo.addItem(label, k)
        self._select_data(self.enc_combo, self.s.encoder)
        self.enc_combo.currentIndexChanged.connect(lambda i: self.set("encoder", self.enc_combo.itemData(i)))
        cl.addWidget(make_row("Encoder", self.enc_combo, "GPU encoders use almost no CPU"))
        col.addWidget(card)

        # ---- Audio
        outs, mics = audio.list_devices()
        card, cl = make_card("Audio")
        self.sys_toggle = Toggle(self.s.system_audio)
        self.sys_toggle.toggled.connect(lambda v: self.set("system_audio", v))
        cl.addWidget(make_row("Game / system sound", self.sys_toggle, "Everything you hear from your speakers or headset"))
        self.sys_dev = QComboBox()
        self.sys_dev.addItem("Default output device", "")
        for n in outs:
            self.sys_dev.addItem(n, n)
        self._select_data(self.sys_dev, self.s.system_device)
        self.sys_dev.currentIndexChanged.connect(lambda i: self.set("system_device", self.sys_dev.itemData(i)))
        self.sys_dev.setMinimumWidth(190)
        cl.addWidget(make_row("Output device", self.sys_dev))
        self.sys_vol = self._slider(self.s.system_volume, "system_volume")
        cl.addWidget(make_row("System volume", self.sys_vol))
        cl.addWidget(separator())

        self.mic_toggle = Toggle(self.s.mic_audio)
        self.mic_toggle.toggled.connect(lambda v: self.set("mic_audio", v))
        cl.addWidget(make_row("Microphone", self.mic_toggle, "Record your voice together with the game"))
        self.mic_dev = QComboBox()
        self.mic_dev.addItem("Default microphone", "")
        for n in mics:
            self.mic_dev.addItem(n, n)
        self._select_data(self.mic_dev, self.s.mic_device)
        self.mic_dev.currentIndexChanged.connect(lambda i: self.set("mic_device", self.mic_dev.itemData(i)))
        self.mic_dev.setMinimumWidth(190)
        cl.addWidget(make_row("Input device", self.mic_dev))
        self.mic_vol = self._slider(self.s.mic_volume, "mic_volume")
        cl.addWidget(make_row("Mic volume", self.mic_vol))
        cl.addWidget(separator())

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-1000, 1000)
        self.offset_spin.setSingleStep(10)
        self.offset_spin.setSuffix(" ms")
        self.offset_spin.setValue(self.s.audio_offset_ms)
        self.offset_spin.valueChanged.connect(lambda v: self.set("audio_offset_ms", v))
        cl.addWidget(make_row("Audio sync offset", self.offset_spin,
                              "If sound is late, use a negative value. If early, a positive one."))
        if not audio.available():
            for w in (self.sys_toggle, self.sys_dev, self.sys_vol, self.mic_toggle, self.mic_dev, self.mic_vol):
                w.setEnabled(False)
        col.addWidget(card)

        # ---- Clips
        card, cl = make_card("Clips")
        self.dir_edit = QLineEdit(self.s.output_dir)
        self.dir_edit.setReadOnly(True)
        browse = QPushButton("Browse")
        browse.setCursor(Qt.PointingHandCursor)
        browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        lbl = QLabel("Save folder")
        cl.addWidget(lbl)
        cl.addLayout(dir_row)

        self.prefix_edit = QLineEdit(self.s.filename_prefix)
        self.prefix_edit.setMaxLength(40)
        self.prefix_edit.setFixedWidth(170)
        self.prefix_edit.editingFinished.connect(
            lambda: self.set("filename_prefix", self._clean_name(self.prefix_edit.text())))
        cl.addWidget(make_row("File name prefix", self.prefix_edit))

        self.buf_spin = QSpinBox()
        self.buf_spin.setRange(1, 30)
        self.buf_spin.setSuffix(" min")
        self.buf_spin.setValue(self.s.buffer_minutes)
        self.buf_spin.valueChanged.connect(self._buffer_minutes_changed)
        cl.addWidget(make_row("Buffer length", self.buf_spin, "How far back you can go"))
        self.disk_label = QLabel()
        self.disk_label.setObjectName("sub")
        self.disk_label.setWordWrap(True)
        cl.addWidget(self.disk_label)

        self.sound_toggle = Toggle(self.s.play_sound)
        self.sound_toggle.toggled.connect(lambda v: self.set("play_sound", v))
        cl.addWidget(make_row("Play sound when saved", self.sound_toggle))
        self.open_toggle = Toggle(self.s.open_folder_after_save)
        self.open_toggle.toggled.connect(lambda v: self.set("open_folder_after_save", v))
        cl.addWidget(make_row("Open folder after saving", self.open_toggle))
        col.addWidget(card)

        # ---- Hotkey + system
        card, cl = make_card("App")
        self.hk_edit = QKeySequenceEdit(QKeySequence(self.s.hotkey))
        self.hk_edit.setMaximumSequenceLength(1)
        self.hk_edit.setFixedWidth(150)
        self.hk_edit.keySequenceChanged.connect(self._hotkey_changed)
        cl.addWidget(make_row("Save hotkey", self.hk_edit, "Click the box, then press the keys"))

        self.auto_buf_toggle = Toggle(self.s.auto_start_buffer)
        self.auto_buf_toggle.toggled.connect(lambda v: self.set("auto_start_buffer", v))
        cl.addWidget(make_row("Start buffering when app opens", self.auto_buf_toggle))

        self.win_toggle = Toggle(autostart.is_enabled() if autostart.is_supported() else False)
        self.win_toggle.toggled.connect(self._autostart_changed)
        self.win_toggle.setEnabled(autostart.is_supported())
        cl.addWidget(make_row("Start with Windows", self.win_toggle, "Runs quietly in the system tray"))

        self.tray_toggle = Toggle(self.s.close_to_tray)
        self.tray_toggle.toggled.connect(lambda v: self.set("close_to_tray", v))
        cl.addWidget(make_row("Keep running when closed", self.tray_toggle, "Closing the window sends it to the tray"))
        col.addWidget(card)
        about = QLabel(f"SK Recorder \u00b7 version {APP_VERSION}")
        about.setObjectName("footer")
        about.setAlignment(Qt.AlignCenter)
        col.addWidget(about)
        col.addStretch(1)
        self._update_disk_label()
        return page

    def _slider(self, value: int, key: str) -> QSlider:
        sl = QSlider(Qt.Horizontal)
        sl.setRange(0, 200)
        sl.setValue(value)
        sl.setFixedWidth(150)
        sl.valueChanged.connect(lambda v: self.set(key, v))
        return sl

    @staticmethod
    def _select_data(combo: QComboBox, data) -> None:
        i = combo.findData(data)
        combo.setCurrentIndex(i if i >= 0 else 0)

    @staticmethod
    def _clean_name(text: str) -> str:
        bad = '<>:"/\\|?*'
        text = "".join(c for c in text if c not in bad).strip()
        return text or "SK Clip"

    # ================================================================= TRAY
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(theme.make_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        a_save = QAction("Save clip now", self)
        a_save.triggered.connect(self.do_save)
        a_show = QAction("Open SK Recorder", self)
        a_show.triggered.connect(self.show_window)
        a_dir = QAction("Open clips folder", self)
        a_dir.triggered.connect(self.open_folder)
        self.a_toggle = QAction("Pause buffer", self)
        self.a_toggle.triggered.connect(self._master_clicked)
        a_quit = QAction("Quit", self)
        a_quit.triggered.connect(self.quit_app)
        for a in (a_save, a_show, a_dir):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.a_toggle)
        menu.addSeparator()
        menu.addAction(a_quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_window() if r == QSystemTrayIcon.Trigger else None)
        self.tray.messageClicked.connect(lambda: reveal_in_folder(self._last_clip) if self._last_clip else None)
        self.tray.show()

    def show_window(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        self._quitting = True
        self.hotkeys.stop()
        self.engine.stop()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if not self._quitting and self.s.close_to_tray and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Still recording in the background. Right-click the tray icon to quit.",
                                      QSystemTrayIcon.Information, 4000)
            return
        self.quit_app()
        event.accept()

    # ================================================================= ACTIONS
    def do_save(self) -> None:
        if not self.engine.is_running():
            self._set_message("The replay buffer is off. Turn it on first.", "error")
            return
        self.save_btn.setEnabled(False)
        self.save_btn.setText("Saving…")
        self.engine.save_clip(self.s.clip_seconds)

    def open_folder(self) -> None:
        Path(self.s.output_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.s.output_dir))

    def _master_clicked(self, *_a) -> None:
        if self.engine.is_running():
            self.engine.stop()
        else:
            self.engine.start()

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose where to save clips", self.s.output_dir)
        if d:
            self.dir_edit.setText(d)
            self.set("output_dir", d)

    def _chip_clicked(self, idx: int) -> None:
        val = self.chip_values[idx]
        if val == 0:
            self.custom_row.show()
            self._custom_changed()
        else:
            self.custom_row.hide()
            self.set("clip_seconds", min(val, self.s.buffer_minutes * 60))

    def _custom_changed(self, *_a) -> None:
        total = self.custom_min.value() * 60 + self.custom_sec.value()
        total = max(5, min(total, self.s.buffer_minutes * 60))
        self.set("clip_seconds", total)

    def _buffer_minutes_changed(self, v: int) -> None:
        self.set("buffer_minutes", v)
        if self.s.clip_seconds > v * 60:
            self.set("clip_seconds", v * 60)
        self._sync_clip_controls()
        self._update_disk_label()

    def _hotkey_changed(self, seq: QKeySequence) -> None:
        text = seq.toString()
        if not text:
            return
        if self.hotkeys.set_hotkey(text):
            self.set("hotkey", text)
            self._set_message(f"Hotkey set to {text}.", "ok")
        else:
            self._set_message(f"Could not use that hotkey: {self.hotkeys.error}", "error")
            self.hotkeys.set_hotkey(self.s.hotkey)
        self._refresh_hotkey_label()

    def _autostart_changed(self, on: bool) -> None:
        if not autostart.set_enabled(on):
            self._set_message("Could not change the Windows startup entry.", "error")
            self.win_toggle.blockSignals(True)
            self.win_toggle.setChecked(not on)
            self.win_toggle.blockSignals(False)
            self.win_toggle.update()
        self.set("start_with_windows", autostart.is_enabled())

    def set(self, key: str, value) -> None:
        if getattr(self.s, key) == value:
            return
        setattr(self.s, key, value)
        config.save(self.s)
        if key in ("fps", "quality", "buffer_minutes"):
            self._update_disk_label()
        if key == "clip_seconds":
            self._refresh_save_text()
        if key in Settings.CAPTURE_KEYS and self.engine.is_running():
            self._restart_timer.start()
            self._set_message("Applying changes…", "hint")

    def _apply_capture_changes(self) -> None:
        if self.engine.is_running():
            self.engine.restart()

    # ================================================================= STATE / REFRESH
    def _on_state(self, state: str, msg: str) -> None:
        names = {E.STOPPED: "Stopped", E.STARTING: "Starting…", E.RECORDING: "Buffering", E.ERROR: "Error"}
        self.pill.setText(names.get(state, state))
        self.pill.setProperty("state", state)
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)
        running = state in (E.STARTING, E.RECORDING)
        self.master_toggle.blockSignals(True)
        self.master_toggle.setChecked(running)
        self.master_toggle.blockSignals(False)
        self.master_toggle.update()
        self.a_toggle.setText("Pause buffer" if running else "Start buffer")
        self.save_btn.setEnabled(state == E.RECORDING)
        self._refresh_save_text()
        if state == E.ERROR:
            self._set_message(f"Recording problem: {msg}", "error")
        elif state == E.RECORDING and msg and not msg.startswith("Recording ("):
            self._set_message(msg, "warn")
        elif state == E.RECORDING:
            self._set_message("", "hint")
        elif state == E.STOPPED:
            self._set_message("", "hint")
        self._refresh_buffer()

    def _on_saved(self, path: str, dur: float) -> None:
        self._last_clip = path
        self.reveal_btn.show()
        self.save_btn.setEnabled(self.engine.state == E.RECORDING)
        self._refresh_save_text()
        name = Path(path).name
        self._set_message(f"Saved · {name} ({fmt_time(dur)})", "ok")
        self.tray.showMessage("Clip saved", f"{name}\nClick to show in folder.", QSystemTrayIcon.Information, 4000)
        if self.s.play_sound and sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass
        if self.s.open_folder_after_save:
            reveal_in_folder(path)

    def _on_failed(self, msg: str) -> None:
        self.save_btn.setEnabled(self.engine.state == E.RECORDING)
        self._refresh_save_text()
        self._set_message(f"Could not save: {msg}", "error")

    def _set_message(self, text: str, kind: str) -> None:
        color = {"ok": theme.OK, "warn": theme.WARN, "error": theme.DANGER}.get(kind, theme.MUTED)
        self.note.setStyleSheet(f"color: {color};")
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _refresh_save_text(self) -> None:
        if self.save_btn.text().startswith("Saving") and not self.save_btn.isEnabled():
            return
        self.save_btn.setText(f"Save last {fmt_len(self.s.clip_seconds)}")

    def _refresh_hotkey_label(self) -> None:
        keys = "".join(
            f'<span style="background:{theme.CARD_HI}; border:1px solid {theme.BORDER}; border-radius:5px; '
            f'padding:2px 7px; color:{theme.TEXT}; font-weight:600;">{k}</span>&nbsp;'
            for k in self.s.hotkey.split("+")
        )
        self.hotkey_label.setText(f"Press {keys} anywhere to save")

    def _refresh_buffer(self) -> None:
        total = self.s.buffer_minutes * 60
        have = self.engine.buffered_seconds()
        self.buffer_bar.setRange(0, total)
        self.buffer_bar.setValue(int(have))
        if self.engine.is_running():
            self.buffer_text.setText(fmt_time(have))
            self.buffer_sub.setText(f"of {fmt_time(total)} · ready to save up to {fmt_len(int(min(have, self.s.clip_seconds)))}")
        else:
            self.buffer_text.setText("Off")
            self.buffer_sub.setText("Turn the switch on to start keeping the last "
                                    f"{self.s.buffer_minutes} min of your screen.")

    def _sync_clip_controls(self) -> None:
        cs = self.s.clip_seconds
        self.custom_min.setRange(0, self.s.buffer_minutes)
        idx = self.chip_values.index(cs) if cs in self.chip_values[:-1] else len(self.chip_values) - 1
        self.chip_group.button(idx).setChecked(True)
        self.custom_row.setVisible(idx == len(self.chip_values) - 1)
        self.custom_min.blockSignals(True)
        self.custom_sec.blockSignals(True)
        self.custom_min.setValue(cs // 60)
        self.custom_sec.setValue(cs % 60)
        self.custom_min.blockSignals(False)
        self.custom_sec.blockSignals(False)

    def _update_disk_label(self) -> None:
        mb = self.s.estimated_buffer_mb()
        size = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"
        self.disk_label.setText(f"Temporary disk space used by the buffer: about {size}.")

    def _refresh_all(self) -> None:
        self._sync_clip_controls()
        self._refresh_save_text()
        self._refresh_hotkey_label()
        self._refresh_buffer()
        self.note.hide()

    def _check_probe(self) -> None:
        if not self.engine._probed.is_set():
            return
        self._probe_timer.stop()
        model = self.enc_combo.model()
        for i in range(self.enc_combo.count()):
            key = self.enc_combo.itemData(i)
            if key not in ("auto",) and not self.engine.encoders.get(key, False):
                self.enc_combo.setItemText(i, ENCODER_LABELS[key] + "  (not available)")
                item = model.item(i)
                if item is not None:
                    item.setEnabled(False)
