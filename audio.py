"""System-audio (WASAPI loopback) + microphone capture, mixed to 48 kHz stereo PCM."""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections import deque
from typing import BinaryIO, Optional

import numpy as np

try:  # Windows only
    import pyaudiowpatch as pyaudio
except Exception:  # pragma: no cover
    pyaudio = None

OUT_RATE = 48000
# Keep at most two seconds queued per source. If the encoder/disk stalls, old
# audio is discarded and replaced by silence instead of growing RAM forever.
MAX_BACKLOG_S = 2.0


def available() -> bool:
    return pyaudio is not None


def _wasapi(p):
    return p.get_host_api_info_by_type(pyaudio.paWASAPI)


def _boost_thread() -> None:
    """Audio delivery must not be starved by the (below-normal) encoder."""
    if sys.platform != "win32":
        return
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentThread.restype = wintypes.HANDLE
        k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        k32.SetThreadPriority.restype = wintypes.BOOL
        k32.SetThreadPriority(k32.GetCurrentThread(), 1)  # THREAD_PRIORITY_ABOVE_NORMAL
    except Exception:
        pass


def list_devices() -> tuple[list[str], list[str]]:
    """Return (output device names usable for loopback, microphone names)."""
    if pyaudio is None:
        return [], []
    outs: list[str] = []
    mics: list[str] = []
    p = None
    try:
        p = pyaudio.PyAudio()
        host = _wasapi(p)["index"]
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != host:
                continue
            name = str(d["name"])
            if d.get("isLoopbackDevice"):
                name = name.replace(" [Loopback]", "")
                if name not in outs:
                    outs.append(name)
            elif int(d["maxInputChannels"]) > 0 and name not in mics:
                mics.append(name)
    except Exception:
        pass
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
    return outs, mics


class _Source:
    """Thread-safe FIFO that converts whatever the device gives us to 48 kHz stereo float."""

    def __init__(self, rate: int, channels: int):
        self.rate = max(1, int(rate))
        self.channels = max(1, int(channels))
        self.lock = threading.Lock()
        self.chunks: deque[np.ndarray] = deque()
        self.buf = np.zeros((0, 2), np.float32)
        self.pos = 0.0
        self._queued = 0  # frames waiting in self.chunks

    def push(self, raw: bytes) -> None:
        a = np.frombuffer(raw, np.float32)
        if self.channels > 1:
            a = a[: (len(a) // self.channels) * self.channels].reshape(-1, self.channels)
            a = a[:, :2]
        else:
            a = a.reshape(-1, 1)
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        # Copy to normal float32 so the short-lived PyAudio buffer can be freed.
        a = np.ascontiguousarray(a, dtype=np.float32)
        with self.lock:
            self.chunks.append(a)
            self._queued += len(a)
            limit = int(self.rate * MAX_BACKLOG_S)
            while self._queued > limit and len(self.chunks) > 1:
                old = self.chunks.popleft()
                self._queued -= len(old)
            if self._queued > limit and self.chunks:
                drop = self._queued - limit
                self.chunks[0] = self.chunks[0][drop:]
                self._queued -= drop

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), np.float32)
        with self.lock:
            if self.chunks:
                if len(self.chunks) == 1 and self.buf.size == 0:
                    self.buf = self.chunks[0]
                else:
                    self.buf = np.concatenate([self.buf, *self.chunks], axis=0)
                self.chunks.clear()
                self._queued = 0

            limit = int(self.rate * MAX_BACKLOG_S)
            if len(self.buf) > limit:  # we fell behind: drop oldest audio instead of lagging
                self.buf = self.buf[-limit:]
                self.pos = 0.0

            step = self.rate / OUT_RATE
            if step == 1.0:
                m = min(n, len(self.buf))
                if m:
                    out[:m] = self.buf[:m]
                    self.buf = self.buf[m:]
            else:
                room = len(self.buf) - 2 - self.pos
                m = min(n, int(room / step) + 1) if room >= 0 else 0
                if m > 0:
                    idx = self.pos + np.arange(m, dtype=np.float64) * step
                    i0 = idx.astype(np.int64)
                    fr = (idx - i0).astype(np.float32)[:, None]
                    out[:m] = self.buf[i0] * (1.0 - fr) + self.buf[i0 + 1] * fr
                    newpos = self.pos + m * step
                    drop = int(newpos)
                    if drop:
                        self.buf = self.buf[drop:]
                    self.pos = newpos - drop
        return out


class AudioCapture:
    def __init__(self, system: bool, system_device: str, system_volume: int,
                 mic: bool, mic_device: str, mic_volume: int):
        self.system = system
        self.system_device = system_device
        self.system_gain = system_volume / 100.0
        self.mic = mic
        self.mic_device = mic_device
        self.mic_gain = mic_volume / 100.0
        self.warnings: list[str] = []
        self._p = None
        self._streams = []
        self._sys_src: Optional[_Source] = None
        self._mic_src: Optional[_Source] = None

    def _find_loopback(self):
        p = self._p
        host = _wasapi(p)
        default_out = p.get_device_info_by_index(host["defaultOutputDevice"])
        wanted = (self.system_device or str(default_out["name"])).strip()
        wanted_l = wanted.lower()
        fallback = None
        for lb in p.get_loopback_device_info_generator():
            name = str(lb["name"])
            clean = name.replace(" [Loopback]", "")
            if clean.lower() == wanted_l:
                return lb
            if wanted_l in clean.lower() and fallback is None:
                fallback = lb
        if fallback:
            return fallback
        # If the configured device vanished, prefer the default output loopback.
        for lb in p.get_loopback_device_info_generator():
            if str(default_out["name"]).lower() in str(lb["name"]).lower():
                return lb
        raise RuntimeError("No loopback device found for system audio.")

    def _find_mic(self):
        p = self._p
        host = _wasapi(p)
        if self.mic_device:
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if (d["hostApi"] == host["index"] and int(d["maxInputChannels"]) > 0
                        and not d.get("isLoopbackDevice") and str(d["name"]) == self.mic_device):
                    return d
        idx = int(host["defaultInputDevice"])
        if idx < 0:
            raise RuntimeError("No microphone found.")
        return p.get_device_info_by_index(idx)

    def _open(self, dev, src: _Source):
        def cb(data, frames, t, status):
            try:
                src.push(data)
            except Exception:
                pass
            return (None, pyaudio.paContinue)

        ch = max(1, int(dev["maxInputChannels"]))
        st = self._p.open(
            format=pyaudio.paFloat32, channels=ch, rate=int(dev["defaultSampleRate"]),
            input=True, input_device_index=dev["index"], frames_per_buffer=1024,
            stream_callback=cb,
        )
        self._streams.append(st)

    def start(self) -> None:
        if pyaudio is None:
            raise RuntimeError("Audio capture needs the 'PyAudioWPatch' package (Windows).")
        self._p = pyaudio.PyAudio()
        errors: list[str] = []
        try:
            if self.system:
                try:
                    dev = self._find_loopback()
                    src = _Source(int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"])))
                    self._open(dev, src)
                    self._sys_src = src
                except Exception as e:
                    errors.append(f"System audio: {e}")
            if self.mic:
                try:
                    dev = self._find_mic()
                    src = _Source(int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"])))
                    self._open(dev, src)
                    self._mic_src = src
                except Exception as e:
                    errors.append(f"Microphone: {e}")
            if not self._streams:
                raise RuntimeError("; ".join(errors) or "No audio input could be opened.")
            self.warnings.extend(errors)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for st in self._streams:
            try:
                st.stop_stream()
                st.close()
            except Exception:
                pass
        self._streams = []
        if self._p is not None:
            try:
                self._p.terminate()
            except Exception:
                pass
            self._p = None

    def mix(self, n: int) -> bytes:
        acc = np.zeros((n, 2), np.float32)
        if self._sys_src is not None:
            acc += self._sys_src.pull(n) * self.system_gain
        if self._mic_src is not None:
            acc += self._mic_src.pull(n) * self.mic_gain
        np.clip(acc, -1.0, 1.0, out=acc)
        return (acc * 32767.0).astype("<i2").tobytes()


class Feeder(threading.Thread):
    """Writes real-time 48 kHz PCM to ffmpeg's stdin (silence when nothing is playing)."""

    MAX_CHUNK = OUT_RATE // 10  # never allocate/write more than 100 ms at once

    def __init__(self, capture: Optional[AudioCapture], sink: BinaryIO):
        super().__init__(daemon=True, name="sk-audio-feeder")
        self.capture = capture
        self.sink = sink
        self._halt = threading.Event()
        self._mix_failed = False

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        _boost_thread()
        t0 = time.perf_counter()
        written = 0
        while not self._halt.is_set():
            target = int((time.perf_counter() - t0) * OUT_RATE)
            need = target - written
            if need < OUT_RATE // 50:  # batch at least 20 ms of audio
                self._halt.wait(0.005)
                continue
            need = min(need, self.MAX_CHUNK)
            if self.capture is not None:
                try:
                    data = self.capture.mix(need)
                except Exception as e:
                    if not self._mix_failed:
                        self._mix_failed = True
                        try:
                            import ffmpeg_utils as fu
                            fu.log_exception("audio mix failed; writing silence", e)
                        except Exception:
                            pass
                    data = bytes(need * 4)
            else:
                data = bytes(need * 4)
            try:
                self.sink.write(data)
                self.sink.flush()
            except (BrokenPipeError, OSError, ValueError):
                break
            written += need
