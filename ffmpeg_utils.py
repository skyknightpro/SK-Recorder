"""FFmpeg helpers: locate/download, encoder detection, command building, clip assembly."""
from __future__ import annotations

import ctypes
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from config import Settings, data_dir

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
BELOW_NORMAL_PRIORITY = 0x00004000 if sys.platform == "win32" else 0
# The capture/encode processes must never fight the game for CPU time. The UI
# process itself stays at normal priority (the old build lowered everything,
# which could starve audio and make the window feel unresponsive).
CAPTURE_FLAGS = CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY
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


# --------------------------------------------------------------------------- job object
# Every ffmpeg process is assigned to a Windows job with KILL_ON_JOB_CLOSE. If
# SK Recorder ever crashes or is killed, Windows also kills the recorder instead
# of leaving a hidden ffmpeg.exe writing to the disk forever.
_JOB_LOCK = threading.Lock()
_JOB_READY = False
_JOB_HANDLE = None
_K32 = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _job_handle():
    global _JOB_READY, _JOB_HANDLE, _K32
    if sys.platform != "win32":
        return None
    with _JOB_LOCK:
        if _JOB_READY:
            return _JOB_HANDLE
        _JOB_READY = True
        try:
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                    handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(handle)
                return None
            _JOB_HANDLE, _K32 = handle, k32
        except Exception:
            _JOB_HANDLE, _K32 = None, None
        return _JOB_HANDLE


def attach_job(proc: subprocess.Popen) -> None:
    """Best-effort: tie a child process to this app's lifetime."""
    if sys.platform != "win32":
        return
    handle = _job_handle()
    if not handle or _K32 is None:
        return
    try:
        _K32.AssignProcessToJobObject(handle, int(proc._handle))
    except Exception:
        pass


# --------------------------------------------------------------------------- paths / logging
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


_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    """Tiny rolling log next to the settings (Settings > Open log)."""
    try:
        p = data_dir() / "log.txt"
        with _LOG_LOCK:
            try:
                if p.exists() and p.stat().st_size > 1_000_000:
                    os.replace(p, p.with_name("log.old.txt"))
            except OSError:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def log_exception(prefix: str, exc: BaseException) -> None:
    try:
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        text = repr(exc)
    log(prefix + "\n" + text.strip())


# --------------------------------------------------------------------------- ffmpeg probing
def _run(args: list[str], timeout: float = 15, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CAPTURE_FLAGS,
        cwd=cwd,
    )
    attach_job(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=3)
        except Exception:
            out, err = "", ""
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def ffmpeg_works(path: str) -> bool:
    try:
        r = _run([path, "-hide_banner", "-version"], timeout=10)
        text = ((r.stdout or "") + (r.stderr or "")).lower()
        return r.returncode == 0 and "ffmpeg version" in text
    except (OSError, subprocess.SubprocessError):
        return False


def _encoder_names(ffmpeg: str) -> Optional[set[str]]:
    try:
        out = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=12).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names or None


def has_filter(ffmpeg: str, name: str) -> bool:
    try:
        out = _run([ffmpeg, "-hide_banner", "-filters"], timeout=12).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def test_encoder(ffmpeg: str, key: str) -> bool:
    enc = ENCODER_NAMES[key]
    fmt = "yuv420p" if key == "cpu" else "nv12"
    try:
        r = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30",
            "-vf", f"format={fmt}", "-frames:v", "3", "-c:v", enc, "-f", "null", "-",
        ], timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def test_zero_copy(ffmpeg: str, key: str) -> bool:
    """Can desktop frames go straight from the GPU into the hardware encoder (no CPU copy)?"""
    if key not in ("nvenc", "amf"):
        return False
    try:
        r = _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "ddagrab=framerate=30",
            "-frames:v", "3", "-c:v", ENCODER_NAMES[key], "-f", "null", "-",
        ], timeout=14)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_encoders(ffmpeg: str) -> dict[str, bool]:
    names = _encoder_names(ffmpeg)
    result: dict[str, bool] = {}
    for k in ("nvenc", "amf", "qsv", "cpu"):
        if names is not None and ENCODER_NAMES[k] not in names:
            result[k] = False
        else:
            result[k] = test_encoder(ffmpeg, k)
    return result


def resolve_encoder(setting: str, available: dict[str, bool]) -> str:
    if setting != "auto" and available.get(setting):
        return setting
    for k in AUTO_ORDER:
        if available.get(k):
            return k
    return "cpu"


def can_zero_copy(s: Settings, encoder: str, grabber: str) -> bool:
    """Frames can stay on the GPU (no download/upload) for NVENC/AMF when no scaling is needed."""
    return grabber == "ddagrab" and encoder in ("nvenc", "amf") and not s.resolution


def cpu_threads() -> int:
    # Leave plenty of headroom for the game; x264 gains little past 8 threads.
    return max(2, min(8, (os.cpu_count() or 8) // 2))


# --------------------------------------------------------------------------- capture command
def build_capture_command(
    ffmpeg: str, s: Settings, encoder: str, grabber: str, with_audio: bool, buffer_dir: Path,
    mode: str = "download",
) -> list[str]:
    """Build the ffmpeg command that records continuously into rolling .ts segments.

    mode "zerocopy": desktop frames stay on the GPU and go directly into NVENC/AMF
                     (no download to RAM, no CPU colour conversion) - the lightest path.
    mode "download": frames are copied to RAM (needed for scaling / QSV / CPU encoding).
    """
    fps = max(1, int(s.fps))
    zero = mode == "zerocopy" and grabber == "ddagrab" and encoder in ("nvenc", "amf")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
    if not with_audio:
        cmd.append("-nostdin")

    # --- video input ---
    if grabber == "ddagrab":
        src = f"ddagrab=output_idx={max(0, int(s.monitor))}:framerate={fps}:draw_mouse=1"
        if not zero:
            src += ",hwdownload,format=bgra"
        cmd += ["-f", "lavfi", "-i", src]
    else:  # gdigrab: whole virtual desktop
        cmd += [
            "-thread_queue_size", "16", "-f", "gdigrab", "-framerate", str(fps),
            "-draw_mouse", "1", "-i", "desktop",
        ]

    # --- audio input (raw PCM mixed by the app, piped through stdin) ---
    if with_audio:
        cmd += ["-thread_queue_size", "1024"]
        if s.audio_offset_ms:
            cmd += ["-itsoffset", f"{s.audio_offset_ms / 1000:.3f}"]
        cmd += ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"]

    cmd += ["-map", "0:v:0"]
    if with_audio:
        cmd += ["-map", "1:a:0"]

    # --- video filters ---
    if not zero:
        vf = []
        if s.resolution:
            vf.append(f"scale=-2:min(ih\\,{int(s.resolution)}):flags=fast_bilinear")
        vf.append("format=yuv420p" if encoder == "cpu" else "format=nv12")
        cmd += ["-vf", ",".join(vf)]

    # --- encoder ---
    kbps = s.video_kbps()
    gop = max(1, fps * SEG_TIME)
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.25)}k", "-bufsize", f"{kbps * 2}k"]
    if encoder == "nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p3", "-tune", "ll", "-rc", "vbr", *rate,
                "-bf", "0", "-profile:v", "high", "-forced-idr", "1"]
    elif encoder == "amf":
        cmd += ["-c:v", "h264_amf", "-quality", "speed", "-rc", "vbr_peak", *rate, "-profile:v", "high"]
    elif encoder == "qsv":
        cmd += ["-c:v", "h264_qsv", "-preset", "veryfast", *rate]
    else:
        cmd += ["-c:v", "libx264", "-preset", "superfast", "-threads", str(cpu_threads()),
                "-sc_threshold", "0", *rate]
    cmd += ["-g", str(gop), "-force_key_frames", f"expr:gte(t,n_forced*{SEG_TIME})"]

    # --- audio encoder ---
    if with_audio:
        cmd += ["-af", "aresample=async=1000", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]

    # --- rolling segments ---
    # Cut slightly before SEG_TIME. Keyframes are forced every SEG_TIME, and
    # FFmpeg's segmenter skips a cut when the gap is exactly SEG_TIME (it
    # compares against a floating-point value). Without this margin the first
    # segment is 4 s / 6 s / 8 s long instead of 2 s.
    cmd += [
        "-max_muxing_queue_size", "2048",
        "-f", "segment", "-segment_time", f"{SEG_TIME - 0.1:g}", "-segment_format", "mpegts",
        "-reset_timestamps", "1", "-segment_start_number", "0",
        str(buffer_dir / "seg_%06d.ts"),
    ]
    return cmd


# --------------------------------------------------------------------------- buffer files
def list_segments(buffer_dir: Path) -> list[Path]:
    try:
        files = sorted(buffer_dir.glob("seg_*.ts"))
    except OSError:
        return []
    out: list[Path] = []
    for p in files:
        try:
            if p.stat().st_size > 0:
                out.append(p)
        except OSError:
            pass
    return out


def clear_segments(buffer_dir: Path) -> None:
    try:
        buffer_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for p in list_segments(buffer_dir):
        try:
            p.unlink()
        except OSError:
            pass


def sanitize_filename(text: object, fallback: str = "SK Clip") -> str:
    text = str(text or "")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = " ".join(text.split()).strip(" .")
    return (text[:80].rstrip(" .") or fallback)


def unique_path(folder: Path, base: str, ext: str = ".mp4") -> Path:
    p = folder / f"{base}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{base} ({n}){ext}"
        n += 1
    return p


def media_duration(ffmpeg: str, path: Path) -> float:
    """Read duration from the container header (fast)."""
    try:
        r = _run([ffmpeg, "-hide_banner", "-i", str(path)], timeout=12)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    if not m:
        return 0.0
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except ValueError:
        return 0.0


def _tail_decodes(ffmpeg: str, path: Path) -> bool:
    """Check the last ~2 s of a clip for a truncated final frame.

    The newest segment is a snapshot of a file that is still being written, so
    its very last frame can be incomplete. Decoding only the tail is cheap even
    for long clips."""
    try:
        r = _run([
            ffmpeg, "-hide_banner", "-v", "error", "-sseof", "-1",
            "-i", str(path), "-map", "0:v:0", "-f", "null", "-",
        ], timeout=30)
    except Exception:
        return True  # never block a save on a diagnostic
    return not (r.stderr or "").strip()


def assemble_clip(
    ffmpeg: str,
    segments: list[Path],
    seconds: int,
    out_path: Path,
    on_staged: Optional[Callable[[], None]] = None,
) -> float:
    """Join the newest whole segments into an MP4, losslessly.

    Every segment starts on a keyframe, so keeping whole segments guarantees a
    clean, decodable first frame and always includes the very latest footage.
    The number of segments is chosen so the clip is between ``seconds`` and
    ``seconds + 2*SEG_TIME`` long. No stream re-encode and no second temporary
    file are needed.

    The newest segment is copied instead of hard-linked: it is still being
    written, and a copy captures exactly the frames that existed when Save was
    pressed. Older segments are hard-linked (nearly free) and the rolling buffer
    is free to delete its originals as soon as staging is done.

    Returns the clip duration in seconds.
    """
    if not segments:
        raise RuntimeError("The replay buffer is empty - wait a couple of seconds after it starts.")

    seconds = max(1, int(seconds))
    need = math.ceil(seconds / SEG_TIME) + 2
    chosen = segments[-need:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="skclip_", dir=str(data_dir())))
    part = out_path.with_name(
        f".{out_path.stem}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.part.mp4"
    )
    try:
        staged: list[Path] = []
        newest = len(chosen) - 1
        for i, seg in enumerate(chosen):
            dst = stage / f"part_{i:04d}.ts"
            try:
                if i == newest:
                    shutil.copyfile(seg, dst)  # stable snapshot of the active segment
                else:
                    os.link(seg, dst)
            except OSError:
                try:
                    shutil.copyfile(seg, dst)
                except OSError:
                    continue  # segment was pruned between snapshot and staging
            staged.append(dst)
        if not staged:
            raise RuntimeError("The buffered segments are no longer available. Please try again.")

        # The newest segment is usually partial. Its real length tells us how
        # many older (full-length) segments are needed to reach `seconds`.
        newest_len = media_duration(ffmpeg, staged[-1])
        if newest_len <= 0:
            # Unknown length (the active segment just started): add an extra
            # segment. A slightly longer clip is better than a short one.
            newest_len = 0.0
        count = math.ceil(max(0.0, seconds - newest_len) / SEG_TIME) + 1
        count = max(1, min(count, len(staged)))
        used = staged[-count:]
        log(f"assemble: staged={len(staged)} used={count} newest={newest_len:.2f}s "
            f"target={seconds}s")

        # Staging is complete (older segments are hard-linked), so the rolling
        # buffer may now delete the originals while FFmpeg assembles the clip.
        if on_staged is not None:
            try:
                on_staged()
            except Exception:
                pass

        def run_concat(files: list[Path]):
            text = "\n".join(f"file '{p.name}'" for p in files) + "\n"
            (stage / "list.txt").write_text(text, encoding="utf-8")
            return _run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "concat", "-safe", "0", "-i", "list.txt",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", "-dn", "-sn",
                "-bsf:a", "aac_adtstoasc", "-avoid_negative_ts", "make_zero",
                "-max_muxing_queue_size", "4096",
                "-movflags", "+faststart",
                str(part),
            ], timeout=300, cwd=str(stage))

        r = run_concat(used)
        bad = r.returncode != 0 or not part.exists() or part.stat().st_size < 1024
        if not bad and len(used) > 1 and not _tail_decodes(ffmpeg, part):
            # The snapshot of the live segment ended in the middle of a frame.
            # Re-join without it: losing the last couple of seconds is better
            # than shipping a visibly damaged clip.
            log("assemble: incomplete tail in the newest segment; retrying without it")
            bad = True
        if bad and len(used) > 1:
            # The active segment is captured while it is still being written.
            # If its tail was incomplete, retry without it: losing at most two
            # seconds is far better than failing the save.
            log("assemble: retrying without the newest segment")
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            used = used[:-1]
            r = run_concat(used)
            bad = r.returncode != 0 or not part.exists() or part.stat().st_size < 1024
        if bad:
            detail = (r.stderr or "").strip()[-500:]
            raise RuntimeError("Could not write the clip." + (("\n" + detail) if detail else ""))
        os.replace(part, out_path)
        dur = media_duration(ffmpeg, out_path)
        if dur <= 0:
            dur = float(max(1.0, len(used) * SEG_TIME))
        return dur
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        shutil.rmtree(stage, ignore_errors=True)


# --------------------------------------------------------------------------- ffmpeg download
def cleanup_stale_temp() -> None:
    """Remove staging folders left behind by a crash or forced shutdown."""
    root = data_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for p in entries:
        try:
            if p.is_dir() and (p.name.startswith("skclip_") or p.name.startswith("skff_")):
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file() and p.name.startswith("config.") and p.name.endswith(".tmp"):
                age = time.time() - p.stat().st_mtime
                if age > 3600:
                    p.unlink()
        except OSError:
            pass


def download_ffmpeg(progress: Callable[[float, str], None]) -> str:
    """Download a Windows FFmpeg build and extract ffmpeg.exe into the user's bin folder."""
    last_err = "unknown error"
    tmp = Path(tempfile.mkdtemp(prefix="skff_", dir=str(data_dir())))
    try:
        for url in FFMPEG_URLS:
            try:
                zpath = tmp / "ffmpeg.zip"
                req = urllib.request.Request(url, headers={"User-Agent": "SKRecorder"})
                with urllib.request.urlopen(req, timeout=45) as resp, open(zpath, "wb") as f:
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
                    tmp_target = target.with_name("ffmpeg.download.exe")
                    with z.open(member) as src, open(tmp_target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(tmp_target, target)
                if not ffmpeg_works(str(target)):
                    raise RuntimeError("The downloaded FFmpeg could not be started.")
                progress(1.0, "Done")
                return str(target)
            except Exception as e:  # try the next mirror
                last_err = str(e)
        raise RuntimeError(f"Could not download FFmpeg ({last_err}).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
