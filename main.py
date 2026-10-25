"""SK Recorder - an always-on replay buffer for your screen.

Run:  python main.py            (add --minimized to start in the tray)
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QEventLoop, QLockFile, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

import config
import ffmpeg_utils as fu
import theme
from config import APP_ID, APP_NAME


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
    if ff:
        return ff
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
    th.start()
    loop.exec()
    th.wait()
    dlg.close()
    if "path" in result:
        return result["path"]
    QMessageBox.critical(None, APP_NAME,
                         f"{result.get('error', 'Download failed.')}\n\n"
                         f"You can also place ffmpeg.exe next to SK Recorder (in a 'bin' folder).")
    return None


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SK.Recorder.App")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    theme.apply_theme(app)
    app.setWindowIcon(theme.make_icon())

    lock = QLockFile(str(config.data_dir() / f"{APP_ID}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(150):
        QMessageBox.information(None, APP_NAME, "SK Recorder is already running.\nLook for its icon in the system tray.")
        return 0

    ff = ensure_ffmpeg()
    if not ff:
        return 1

    # imported late so the lock / FFmpeg checks run before heavy setup
    from ui import MainWindow

    settings = config.load()
    win = MainWindow(ff, settings, start_hidden="--minimized" in sys.argv)
    app.aboutToQuit.connect(lambda: (win.hotkeys.stop(), win.engine.stop()))
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
