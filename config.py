"""Settings storage for SK Recorder."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, asdict, fields
from pathlib import Path

APP_NAME = "SK Recorder"
APP_ID = "SKRecorder"
APP_VERSION = "1.1.0"

_SAVE_LOCK = threading.Lock()
_BAD_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    p = base / APP_ID
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        # A locked-down profile must not prevent the app from starting.
        fallback = Path(tempfile.gettempdir()) / APP_ID
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def config_path() -> Path:
    return data_dir() / "config.json"


def default_output_dir() -> str:
    return str(Path.home() / "Videos" / APP_NAME)


QUALITY_KBPS = {"Low": 5000, "Medium": 10000, "High": 18000, "Ultra": 35000}
FPS_OPTIONS = [24, 30, 60, 120]
RESOLUTIONS = [("Original", 0), ("1440p", 1440), ("1080p", 1080), ("720p", 720), ("480p", 480)]
ENCODER_LABELS = {
    "auto": "Auto (best available)",
    "nvenc": "NVIDIA NVENC",
    "amf": "AMD AMF",
    "qsv": "Intel Quick Sync",
    "cpu": "CPU (x264)",
}


def clean_filename(text: object, fallback: str = "SK Clip", limit: int = 80) -> str:
    text = str(text or "")
    text = _BAD_FILE_CHARS.sub("", text)
    text = " ".join(text.split()).strip(" .")
    return (text[:limit].rstrip(" .") or fallback)


@dataclass
class Settings:
    # clip
    clip_seconds: int = 60
    buffer_minutes: int = 5
    output_dir: str = ""
    filename_prefix: str = "SK Clip"
    hotkey: str = "Alt+F10"
    # video
    monitor: int = 0
    fps: int = 60
    quality: str = "Medium"
    resolution: int = 0          # 0 = keep original, otherwise max height
    encoder: str = "auto"
    # audio
    system_audio: bool = True
    system_device: str = ""      # "" = default output device
    system_volume: int = 100
    mic_audio: bool = False
    mic_device: str = ""         # "" = default microphone
    mic_volume: int = 100
    audio_offset_ms: int = 0     # +ve delays audio, -ve makes audio earlier
    # app
    auto_start_buffer: bool = True
    start_with_windows: bool = False
    close_to_tray: bool = True
    play_sound: bool = True
    open_folder_after_save: bool = False
    auto_recover: bool = True
    schema: int = 2

    def __post_init__(self):
        if not self.output_dir:
            self.output_dir = default_output_dir()

    # settings that need the capture process to be restarted when changed
    CAPTURE_KEYS = (
        "buffer_minutes", "monitor", "fps", "quality", "resolution", "encoder",
        "system_audio", "system_device", "system_volume",
        "mic_audio", "mic_device", "mic_volume", "audio_offset_ms",
    )

    def capture_signature(self) -> tuple:
        return tuple(getattr(self, k) for k in self.CAPTURE_KEYS)

    def video_kbps(self) -> int:
        base = QUALITY_KBPS.get(self.quality, QUALITY_KBPS["Medium"])
        return int(base * (0.75 if self.fps <= 30 else 1.0))

    def estimated_buffer_mb(self) -> int:
        kbps = self.video_kbps() + 192
        return int(kbps * 60 * self.buffer_minutes / 8 / 1024)


def _clamp_int(value, low: int, high: int, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _clamp_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def validate(s: Settings) -> Settings:
    """Keep even a hand-edited / corrupted config from breaking the app."""
    s.buffer_minutes = _clamp_int(s.buffer_minutes, 1, 30, 5)
    s.clip_seconds = _clamp_int(s.clip_seconds, 5, s.buffer_minutes * 60, 60)

    s.monitor = _clamp_int(s.monitor, 0, 15, 0)
    s.fps = s.fps if s.fps in FPS_OPTIONS else 60
    s.quality = s.quality if s.quality in QUALITY_KBPS else "Medium"
    s.resolution = s.resolution if s.resolution in {h for _, h in RESOLUTIONS} else 0
    s.encoder = s.encoder if s.encoder in ENCODER_LABELS else "auto"

    s.system_volume = _clamp_int(s.system_volume, 0, 200, 100)
    s.mic_volume = _clamp_int(s.mic_volume, 0, 200, 100)
    s.audio_offset_ms = _clamp_int(s.audio_offset_ms, -1000, 1000, 0)

    s.output_dir = str(s.output_dir or "").strip() or default_output_dir()
    s.filename_prefix = clean_filename(s.filename_prefix, "SK Clip", 40)
    s.hotkey = str(s.hotkey or "").strip() or "Alt+F10"

    for name in ("system_audio", "mic_audio", "auto_start_buffer", "start_with_windows",
                 "close_to_tray", "play_sound", "open_folder_after_save", "auto_recover"):
        setattr(s, name, _clamp_bool(getattr(s, name), getattr(Settings(), name)))
    return s


def load() -> Settings:
    s = Settings()
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
        # Old configs are validated and clamped field by field. Their capture
        # choices are kept on purpose: a user who already lowered fps/resolution
        # for performance must not have those settings silently reset.
        names = {f.name for f in fields(Settings)}
        for k, v in raw.items():
            if k in names and type(v) is type(getattr(s, k)):
                setattr(s, k, v)
    except (OSError, ValueError):
        pass
    s.schema = 2
    return validate(s)


def save(s: Settings) -> None:
    """Write atomically so a crash/power loss cannot leave a truncated config."""
    try:
        validate(s)
        text = json.dumps(asdict(s), indent=2, ensure_ascii=False)
        tmp = data_dir() / f"config.{os.getpid()}.tmp"
        with _SAVE_LOCK:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, config_path())
    except (OSError, TypeError, ValueError):
        pass
