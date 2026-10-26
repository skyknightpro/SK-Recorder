"""Start SK Recorder together with Windows (HKCU Run key)."""
from __future__ import annotations

import sys
from pathlib import Path

from config import APP_ID

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    script = Path(__file__).resolve().with_name("main.py")
    return f'"{runner}" "{script}" --minimized'


def is_supported() -> bool:
    return sys.platform == "win32"


def set_enabled(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_ID)
            return True
    except OSError:
        return False
