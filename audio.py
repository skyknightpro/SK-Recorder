"""System-audio (WASAPI loopback) + microphone capture, mixed to 48 kHz stereo PCM."""
from __future__ import annotations

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
MAX_BACKLOG_S = 0.5


def available() -> bool:
    return pyaudio is not None


def _wasapi(p):
    return p.get_host_api_info_by_type(pyaudio.paWASAPI)


def list_devices() -> tuple[list[str], list[str]]:
    """Return (output device names usable for loopback, microphone names)."""
    if pyaudio is None:
        return [], []
    outs: list[str] = []
    mics: list[str] = []
    p = pyaudio.PyAudio()
    try:
        host = _wasapi(p)["index"]
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != host:
                continue
            if d.get("isLoopbackDevice"):
                name = d["name"].replace(" [Loopback]", "")
                if name not in outs:
                    outs.append(name)
            elif d["maxInputChannels"] > 0 and d["name"] not in mics:
                mics.append(d["name"])
    except Exception:
        pass
    finally:
        p.terminate()
    return outs, mics


class _Source:
    """Thread-safe FIFO that converts whatever the device gives us to 48 kHz stereo float."""

    def __init__(self, rate: int, channels: int):
        self.rate = rate
        self.channels = channels
        self.lock = threading.Lock()
        self.chunks: deque[np.ndarray] = deque()
        self.buf = np.zeros((0, 2), np.float32)
        self.pos = 0.0

    def push(self, raw: bytes) -> None:
        a = np.frombuffer(raw, np.float32)
        if self.channels > 1:
            a = a[: (len(a) // self.channels) * self.channels].reshape(-1, self.channels)
            a = a[:, :2] if self.channels >= 2 else a
        else:
            a = a.reshape(-1, 1)
        if a.shape[1] == 1:
            a = np.repeat(a, 2, axis=1)
        with self.lock:
            self.chunks.append(a.astype(np.float32, copy=False))

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), np.float32)
        with self.lock:
            if self.chunks:
                self.buf = np.concatenate([self.buf, *self.chunks])
                self.chunks.clear()
            limit = int(self.rate * MAX_BACKLOG_S)
            if len(self.buf) > limit:  # we fell behind: drop oldest audio instead of lagging
                self.buf = self.buf[-limit:]
                self.pos = 0.0
            step = self.rate / OUT_RATE
            if step == 1.0:
                m = min(n, len(self.buf))
                out[:m] = self.buf[:m]
                self.buf = self.buf[m:]
            else:
                room = len(self.buf) - 2 - self.pos
                m = min(n, int(room / step) + 1) if room >= 0 else 0
                if m > 0:
                    idx = self.pos + np.arange(m) * step
                    i0 = idx.astype(np.int64)
                    fr = (idx - i0).astype(np.float32)[:, None]
                    out[:m] = self.buf[i0] * (1 - fr) + self.buf[i0 + 1] * fr
                    newpos = self.pos + m * step
                    drop = int(newpos)
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
        self._p = None
        self._streams = []
        self._sys_src: Optional[_Source] = None
        self._mic_src: Optional[_Source] = None

    def _find_loopback(self):
        p = self._p
        host = _wasapi(p)
        default_out = p.get_device_info_by_index(host["defaultOutputDevice"])
        wanted = self.system_device or default_out["name"]
        fallback = None
        for lb in p.get_loopback_device_info_generator():
            if wanted in lb["name"]:
                return lb
            if default_out["name"] in lb["name"]:
                fallback = lb
        if fallback:
            return fallback
        raise RuntimeError("No loopback device found for system audio.")

    def _find_mic(self):
        p = self._p
        host = _wasapi(p)
        if self.mic_device:
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if (d["hostApi"] == host["index"] and d["maxInputChannels"] > 0
                        and not d.get("isLoopbackDevice") and d["name"] == self.mic_device):
                    return d
        idx = host["defaultInputDevice"]
        if idx < 0:
            raise RuntimeError("No microphone found.")
        return p.get_device_info_by_index(idx)

    def _open(self, dev, src: _Source):
        def cb(data, frames, t, status):
            src.push(data)
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
        try:
            if self.system:
                dev = self._find_loopback()
                self._sys_src = _Source(int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"])))
                self._open(dev, self._sys_src)
            if self.mic:
                dev = self._find_mic()
                self._mic_src = _Source(int(dev["defaultSampleRate"]), max(1, int(dev["maxInputChannels"])))
                self._open(dev, self._mic_src)
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

    def __init__(self, capture: Optional[AudioCapture], sink: BinaryIO):
        super().__init__(daemon=True, name="sk-audio-feeder")
        self.capture = capture
        self.sink = sink
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        t0 = time.perf_counter()
        written = 0
        while not self._halt.is_set():
            target = int((time.perf_counter() - t0) * OUT_RATE)
            need = target - written
            if need < OUT_RATE // 100:  # wait for at least 10 ms of audio
                time.sleep(0.004)
                continue
            if self.capture is not None:
                data = self.capture.mix(need)
            else:
                data = bytes(need * 4)
            try:
                self.sink.write(data)
                self.sink.flush()
            except (OSError, ValueError):
                break
            written += need
