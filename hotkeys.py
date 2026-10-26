"""Global hotkey.

Windows: RegisterHotKey - the OS delivers a message only when the exact combination is pressed.
No keyboard hook, so nothing is inspected on every key press and games are never slowed down.
Other systems (development only): pynput.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_VK = {
    "space": 0x20, "return": 0x0D, "enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "backspace": 0x08, "del": 0x2E, "delete": 0x2E, "ins": 0x2D, "insert": 0x2D, "home": 0x24,
    "end": 0x23, "pgup": 0x21, "pgdown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "pause": 0x13, "print": 0x2C, "scrolllock": 0x91,
}
_MODS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "meta": MOD_WIN, "win": MOD_WIN}

# Common punctuation, used as a fallback when VkKeyScanW is unavailable.
_PUNCT = {
    "]": 0xDD, "[": 0xDB, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "\\": 0xDC, "-": 0xBD, "=": 0xBB, "`": 0xC0,
}


def _char_vk(ch: str) -> tuple[Optional[int], int]:
    """Map a character to (virtual-key, extra modifiers).

    Windows is layout-aware; the small table is only a fallback.
    """
    if ch.isascii() and ch.isalnum():
        return ord(ch.upper()), 0
    if ch in _PUNCT:
        # Use the physical key. VkKeyScanW follows the active keyboard layout,
        # which can map "]" to a completely different key (e.g. Shift+D).
        return _PUNCT[ch], 0
    if sys.platform == "win32":
        try:
            import ctypes
            code = int(ctypes.windll.user32.VkKeyScanW(ord(ch)))
            if code != -1:
                state = (code >> 8) & 0xFF
                extra = 0
                if state & 1:
                    extra |= MOD_SHIFT
                if state & 2:
                    extra |= MOD_CONTROL
                if state & 4:
                    extra |= MOD_ALT
                return code & 0xFF, extra
        except Exception:
            pass
    return _PUNCT.get(ch), 0


def qt_to_win(seq: str) -> Optional[tuple[int, int]]:
    """'Alt+F10' -> (modifiers, virtual-key). None if the sequence is not usable."""
    mods, vk = 0, None
    for part in (p.strip().lower() for p in seq.split("+") if p.strip()):
        if part in _MODS:
            mods |= _MODS[part]
        elif vk is not None:
            return None
        elif part in _VK:
            vk = _VK[part]
        elif part[0] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
        elif len(part) == 1:
            vk, extra = _char_vk(part)
            if vk is None:
                return None
            mods |= extra
        else:
            return None
    if vk is None:
        return None
    # Never register a bare letter/number globally - that would swallow the key
    # for games and every other application. Function keys are fine on their own.
    if not mods and not (0x70 <= vk <= 0x87):
        return None
    return mods, vk


class _WinHotkey(threading.Thread):
    def __init__(self, mods: int, vk: int, cb: Callable[[], None]):
        super().__init__(daemon=True, name="sk-hotkey")
        self.mods, self.vk, self.cb = mods, vk, cb
        self.ready = threading.Event()
        self.cancelled = threading.Event()
        self.ok = False
        self.err = ""
        self.tid = 0

    def run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            self.tid = int(kernel32.GetCurrentThreadId())
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create this thread's message queue
            if self.cancelled.is_set():
                return
            try:
                self.ok = bool(user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk))
                if not self.ok:
                    self.err = "This key combination is already used by another program."
            except Exception as e:
                self.err = str(e)
            self.ready.set()
            if not self.ok:
                return
            try:
                while not self.cancelled.is_set():
                    if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                        break
                    if msg.message == WM_HOTKEY:
                        try:
                            self.cb()
                        except Exception:
                            pass
            finally:
                try:
                    user32.UnregisterHotKey(None, 1)
                except Exception:
                    pass
        finally:
            self.ready.set()

    def stop(self) -> None:
        self.cancelled.set()
        if self.tid:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
            except Exception:
                pass


class HotkeyManager:
    def __init__(self, callback: Callable[[], None]):
        self._cb = callback
        self._win: Optional[_WinHotkey] = None
        self._listener = None
        self.error = ""

    @property
    def supported(self) -> bool:
        return True

    def set_hotkey(self, seq: str) -> bool:
        self.stop()
        self.error = ""
        if sys.platform == "win32":
            parsed = qt_to_win(seq)
            if not parsed:
                self.error = "Invalid hotkey. Add Ctrl/Alt/Shift, or use an F-key."
                return False
            th = _WinHotkey(parsed[0], parsed[1], self._cb)
            th.start()
            if not th.ready.wait(2.5):
                th.stop()
                th.join(1.5)
                self.error = "Timed out while registering the hotkey."
                return False
            if not th.ok:
                self.error = th.err or "Could not register the hotkey."
                th.join(1.0)
                return False
            self._win = th
            return True
        try:  # development fallback on other systems
            from pynput import keyboard as kb
            parts = [("<" + p.lower() + ">") if len(p) > 1 else p.lower() for p in seq.split("+")]
            self._listener = kb.GlobalHotKeys({"+".join(parts): self._cb})
            self._listener.daemon = True
            self._listener.start()
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def stop(self) -> None:
        if self._win is not None:
            self._win.stop()
            self._win.join(1.5)   # must unregister before the same combo is registered again
            self._win = None
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
