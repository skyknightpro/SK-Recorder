"""SK Recorder - an always-on replay buffer for your screen.

Run:  python main.py            (add --minimized to start in the tray)
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback

from PySide6.QtCore import QEventLoop, QLockFile, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

import config
import ffmpeg_utils as fu
import theme
from config import APP_ID, APP_NAME, APP_VERSION

_CRASH_FILE = None


def _install_diagnostics() -> None:
    """Record native crashes and uncaught Python exceptions instead of dying silently."""
    global _CRASH_FILE
    try:
        path = config.data_dir() / "crash.log"
        try:
            if path.exists() and path.stat().st_size > 1_000_000:
                os.replace(path, path.with_name("crash.old.log"))
        except OSError:
            pass
        _CRASH_FILE = open(path, "a", buffering=1, encoding="utf-8")
        try:
            faulthandler.enable(_CRASH_FILE, all_threads=True)
        except TypeError:  # Python 3.11 and older
            faulthandler.enable(_CRASH_FILE)
    except Exception:
        _CRASH_FILE = None

    def write_crash(text: str) -> None:
        try:
            fu.log(text)
        except Exception:
            pass
        if _CRASH_FILE is not None:
            try:
                _CRASH_FILE.write(text + "\n")
                _CRASH_FILE.flush()
            except Exception:
                pass
        if sys.stderr is not None:
            try:
                sys.stderr.write(text + "\n")
            except Exception:
                pass

    def excepthook(etype, value, tb):
        write_crash("UNCAUGHT: " + "".join(traceback.format_exception(etype, value, tb)).strip())

    def thread_excepthook(args):
        write_crash(
            "UNCAUGHT THREAD: " +
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)).strip()
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook


class _Downloader(QThread):
    progress = Signal(float, str)
    done = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.done.emit(fu.download_ffmpeg(lambda f, t: self.progress.emit(f, t)))
        except Exception as e:
            self.failed.emit(str(e))


def ensure_ffmpeg() -> str | None:
    ff = fu.find_ffmpeg()
    if ff and fu.ffmpeg_works(ff):
        return ff
    if ff:
        fu.log(f"ffmpeg at {ff} could not be started; looking for another copy")
    ask = QMessageBox(QMessageBox.Question, APP_NAME,
                      "SK Recorder needs FFmpeg to record video.\n\n"
                      "Download it automatically now? (about 100 MB, one time only)",
                      QMessageBox.Yes | QMessageBox.No)
    if ask.exec() != QMessageBox.Yes:
        return None
    dlg = QProgressDialog("Preparing...", None, 0, 100)
    dlg.setWindowTitle(APP_NAME)
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.show()
    result: dict[str, str] = {}
    loop = QEventLoop()
    th = _Downloader()
    th.progress.connect(lambda f, t: (dlg.setValue(int(f * 100)), dlg.setLabelText(t)))
    th.done.connect(lambda p: (result.update(path=p), loop.quit()))
    th.failed.connect(lambda m: (result.update(error=m), loop.quit()))
    th.finished.connect(loop.quit)  # never hang if the thread dies without a signal
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(lambda: (result.update(error="The FFmpeg download timed out."), loop.quit()))
    guard.start(20 * 60 * 1000)
    th.start()
    loop.exec()
    guard.stop()
    if not th.wait(5000):
        th.terminate()
        th.wait(2000)
    dlg.close()
    if "path" in result:
        return result["path"]
    QMessageBox.critical(None, APP_NAME,
                         f"{result.get('error', 'Download failed.')}\n\n"
                         f"You can also place ffmpeg.exe next to SK Recorder (in a 'bin' folder).")
    return None


def _main() -> int:
    _install_diagnostics()
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SK.Recorder.App")
        except Exception:
            pass
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    theme.apply_theme(app)
    app.setWindowIcon(theme.make_icon())

    lock = QLockFile(str(config.data_dir() / f"{APP_ID}.lock"))
    # Default Qt stale-lock handling: a dead process is detected, and a lock
    # inherited from a crashed/rebooted session is recovered after 30 seconds.
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(150):
        if "--minimized" not in sys.argv:
            QMessageBox.information(None, APP_NAME,
                                    "SK Recorder is already running.\nLook for its icon in the system tray.")
        return 0

    fu.cleanup_stale_temp()
    ff = ensure_ffmpeg()
    if not ff:
        lock.unlock()
        return 1

    # imported late so the lock / FFmpeg checks run before heavy setup
    from ui import MainWindow

    settings = config.load()
    win = MainWindow(ff, settings, start_hidden="--minimized" in sys.argv)
    app.aboutToQuit.connect(win.shutdown)
    code = app.exec()
    try:
        lock.unlock()
    except Exception:
        pass
    return code


def main() -> int:
    try:
        return _main()
    except Exception as e:
        try:
            text = "FATAL: " + "".join(traceback.format_exception(type(e), e, e.__traceback__))
            fu.log(text)
            if _CRASH_FILE is not None:
                _CRASH_FILE.write(text + "\n")
                _CRASH_FILE.flush()
        except Exception:
            pass
        try:
            QMessageBox.critical(None, APP_NAME,
                                 f"SK Recorder could not start:\n\n{e}\n\n"
                                 f"Details were written to:\n{config.data_dir() / 'crash.log'}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
