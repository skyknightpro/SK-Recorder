"""Global hotkey registration (pynput) and Qt key-sequence conversion."""
from __future__ import annotations

from typing import Callable, Optional

try:
    from pynput import keyboard as _kb
except Exception:  # headless / missing
    _kb = None

_MODS = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "meta": "<cmd>", "win": "<cmd>"}
_NAMED = {
    "space": "<space>", "return": "<enter>", "enter": "<enter>", "esc": "<esc>", "escape": "<esc>",
    "tab": "<tab>", "backspace": "<backspace>", "del": "<delete>", "delete": "<delete>",
    "ins": "<insert>", "insert": "<insert>", "home": "<home>", "end": "<end>",
    "pgup": "<page_up>", "pgdown": "<page_down>", "up": "<up>", "down": "<down>",
    "left": "<left>", "right": "<right>", "pause": "<pause>", "print": "<print_screen>",
    "scrolllock": "<scroll_lock>", "capslock": "<caps_lock>",
}


def qt_to_pynput(seq: str) -> Optional[str]:
    """'Alt+F10' -> '<alt>+<f10>'. Returns None if the sequence has no usable key."""
    parts = [p.strip() for p in seq.split("+") if p.strip()]
    if not parts:
        return None
    out = []
    for p in parts:
        low = p.lower()
        if low in _MODS:
            out.append(_MODS[low])
        elif low in _NAMED:
            out.append(_NAMED[low])
        elif low.startswith("f") and low[1:].isdigit():
            out.append(f"<{low}>")
        elif len(low) == 1:
            out.append(low)
        else:
            return None
    if all(o in _MODS.values() for o in out):
        return None
    return "+".join(out)


class HotkeyManager:
    def __init__(self, callback: Callable[[], None]):
        self._cb = callback
        self._listener = None
        self.error = ""

    @property
    def supported(self) -> bool:
        return _kb is not None

    def set_hotkey(self, seq: str) -> bool:
        self.stop()
        self.error = ""
        if _kb is None:
            self.error = "Global hotkeys need the 'pynput' package."
            return False
        combo = qt_to_pynput(seq)
        if not combo:
            self.error = "Invalid hotkey."
            return False
        try:
            self._listener = _kb.GlobalHotKeys({combo: self._cb})
            self._listener.daemon = True
            self._listener.start()
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
