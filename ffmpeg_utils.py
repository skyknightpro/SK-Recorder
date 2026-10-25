"""FFmpeg helpers: locate/download, encoder detection, command building, clip assembly."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from config import Settings, data_dir

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
SEG_TIME = 2  # seconds per rolling segment
EXE = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

ENCODER_NAMES = {
    "nvenc": "h264_nvenc",
    "amf": "h264_amf",
    "qsv": "h264_qsv",
    "cpu": "libx264",
}
AUTO_ORDER = ["nvenc", "amf", "qsv", "cpu"]

FFMPEG_URLS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def user_bin_dir() -> Path:
    p = data_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_ffmpeg() -> Optional[str]:
    bundled = Path(getattr(sys, "_MEIPASS", app_dir()))
    for d in (bundled / "bin", app_dir() / "bin", app_dir(), user_bin_dir()):
        p = d / EXE
        if p.is_file():
            return str(p)
    return shutil.which("ffmpeg")


def _run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def has_filter(ffmpeg: str, name: str) -> bool:
    try:
        out = _run([ffmpeg, "-hide_banner", "-filters"]).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def test_encoder(ffmpeg: str, key: str) -> bool:
    enc = ENCODER_NAMES[key]
    fmt = "yuv420p" if key == "cpu" else "nv12"
    try:
        r = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30",
            "-vf", f"format={fmt}", "-frames:v", "5", "-c:v", enc, "-f", "null", "-",
        ], timeout=12)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_encoders(ffmpeg: str) -> dict[str, bool]:
    return {k: test_encoder(ffmpeg, k) for k in ("nvenc", "amf", "qsv", "cpu")}


def resolve_encoder(setting: str, available: dict[str, bool]) -> str:
    if setting != "auto" and available.get(setting):
        return setting
    for k in AUTO_ORDER:
        if available.get(k):
            return k
    return "cpu"


def build_capture_command(
    ffmpeg: str, s: Settings, encoder: str, grabber: str, with_audio: bool, buffer_dir: Path
) -> list[str]:
    """Build the ffmpeg command that records continuously into rolling .ts segments."""
    fps = s.fps
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]

    # --- video input ---
    if grabber == "ddagrab":
        src = f"ddagrab=output_idx={max(0, s.monitor)}:framerate={fps}:draw_mouse=1,hwdownload,format=bgra"
        cmd += ["-f", "lavfi", "-i", src]
    else:  # gdigrab: whole virtual desktop
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1", "-i", "desktop"]

    # --- audio input (raw PCM mixed by the app, piped through stdin) ---
    if with_audio:
        cmd += ["-thread_queue_size", "4096"]
        if s.audio_offset_ms:
            cmd += ["-itsoffset", f"{s.audio_offset_ms / 1000:.3f}"]
        cmd += ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"]

    cmd += ["-map", "0:v:0"]
    if with_audio:
        cmd += ["-map", "1:a:0"]

    # --- video filters / encoder ---
    vf = []
    if s.resolution:
        vf.append(f"scale=-2:min(ih\\,{s.resolution}):flags=bicubic")
    vf.append("format=yuv420p" if encoder == "cpu" else "format=nv12")
    cmd += ["-vf", ",".join(vf)]

    kbps = s.video_kbps()
    gop = max(1, fps * SEG_TIME)
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.25)}k", "-bufsize", f"{kbps * 2}k"]
    if encoder == "nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", *rate, "-forced-idr", "1"]
    elif encoder == "amf":
        cmd += ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "vbr_peak", *rate]
    elif encoder == "qsv":
        cmd += ["-c:v", "h264_qsv", "-preset", "medium", *rate]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", *rate]
    cmd += [
        "-g", str(gop), "-bf", "2",
        "-force_key_frames", f"expr:gte(t,n_forced*{SEG_TIME})",
    ]

    # --- audio encoder ---
    if with_audio:
        cmd += ["-af", "aresample=async=1000", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]

    # --- rolling segments ---
    cmd += [
        "-f", "segment", "-segment_time", str(SEG_TIME), "-segment_format", "mpegts",
        "-reset_timestamps", "1", "-segment_start_number", "0",
        str(buffer_dir / "seg_%06d.ts"),
    ]
    return cmd


def list_segments(buffer_dir: Path) -> list[Path]:
    try:
        return sorted(buffer_dir.glob("seg_*.ts"))
    except OSError:
        return []


def clear_segments(buffer_dir: Path) -> None:
    buffer_dir.mkdir(parents=True, exist_ok=True)
    for p in list_segments(buffer_dir):
        try:
            p.unlink()
        except OSError:
            pass


def unique_path(folder: Path, base: str, ext: str = ".mp4") -> Path:
    p = folder / f"{base}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{base} ({n}){ext}"
        n += 1
    return p


def assemble_clip(ffmpeg: str, segments: list[Path], seconds: int, out_path: Path) -> float:
    """Join the newest segments into an MP4 and trim it to roughly the last `seconds`.

    Cutting is lossless (no re-encode), so the clip starts on a keyframe and is
    between `seconds` and `seconds + SEG_TIME` long. Returns the estimated duration.
    """
    if not segments:
        raise RuntimeError("The replay buffer is empty - wait a couple of seconds after it starts.")
    need = math.ceil(seconds / SEG_TIME) + 1
    chosen = segments[-need:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="skclip_", dir=str(data_dir())))
    try:
        lines = []
        for i, seg in enumerate(chosen):
            dst = stage / f"part_{i:04d}.ts"
            try:
                os.link(seg, dst)
            except OSError:
                shutil.copyfile(seg, dst)
            lines.append(f"file '{dst.as_posix()}'")
        (stage / "list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        merged = stage / "merged.mp4"
        r = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(stage / "list.txt"),
            "-c", "copy", "-bsf:a", "aac_adtstoasc", "-avoid_negative_ts", "make_zero",
            str(merged),
        ], timeout=180)
        if r.returncode != 0 or not merged.exists():
            raise RuntimeError("Could not join the buffered segments.\n" + (r.stderr or "")[-400:])

        r = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-sseof", f"-{seconds}", "-i", str(merged),
            "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
            str(out_path),
        ], timeout=180)
        if r.returncode != 0 or not out_path.exists():
            raise RuntimeError("Could not write the clip.\n" + (r.stderr or "")[-400:])
        available = len(chosen) * SEG_TIME
        return float(min(seconds + SEG_TIME, available))
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def download_ffmpeg(progress: Callable[[float, str], None]) -> str:
    """Download a Windows FFmpeg build and extract ffmpeg.exe into the user's bin folder."""
    last_err = "unknown error"
    tmp = Path(tempfile.mkdtemp(prefix="skff_", dir=str(data_dir())))
    try:
        for url in FFMPEG_URLS:
            try:
                zpath = tmp / "ffmpeg.zip"
                req = urllib.request.Request(url, headers={"User-Agent": "SKRecorder"})
                with urllib.request.urlopen(req, timeout=30) as resp, open(zpath, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    done = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            progress(done / total * 0.9, f"Downloading FFmpeg... {done // 1048576} MB")
                progress(0.92, "Extracting...")
                with zipfile.ZipFile(zpath) as z:
                    member = next((n for n in z.namelist() if n.endswith("/bin/ffmpeg.exe")), None)
                    if not member:
                        raise RuntimeError("ffmpeg.exe not found in archive")
                    target = user_bin_dir() / "ffmpeg.exe"
                    with z.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                progress(1.0, "Done")
                return str(target)
            except Exception as e:  # try the next mirror
                last_err = str(e)
        raise RuntimeError(f"Could not download FFmpeg ({last_err}).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
