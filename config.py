"""Settings storage for SK Recorder."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, fields
from pathlib import Path

APP_NAME = "SK Recorder"
APP_ID = "SKRecorder"
APP_VERSION = "1.0.0"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    p = base / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


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
    quality: str = "High"
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
        base = QUALITY_KBPS.get(self.quality, QUALITY_KBPS["High"])
        return int(base * (0.75 if self.fps <= 30 else 1.0))

    def estimated_buffer_mb(self) -> int:
        kbps = self.video_kbps() + 192
        return int(kbps * 60 * self.buffer_minutes / 8 / 1024)


def load() -> Settings:
    s = Settings()
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
        names = {f.name for f in fields(Settings)}
        for k, v in raw.items():
            if k in names and type(v) is type(getattr(s, k)):
                setattr(s, k, v)
    except (OSError, ValueError):
        pass
    s.clip_seconds = max(5, min(s.clip_seconds, s.buffer_minutes * 60))
    return s


def save(s: Settings) -> None:
    try:
        config_path().write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
    except OSError:
        pass
