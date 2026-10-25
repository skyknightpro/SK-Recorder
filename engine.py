"""Replay-buffer engine: keeps recording the screen into rolling segments, saves clips on demand.

This module has no Qt dependency. The UI passes plain callbacks.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import audio
import ffmpeg_utils as fu
from config import Settings, data_dir

STOPPED, STARTING, RECORDING, ERROR = "stopped", "starting", "recording", "error"


class ReplayEngine:
    def __init__(
        self,
        ffmpeg: str,
        settings: Settings,
        on_state: Callable[[str, str], None],
        on_saved: Callable[[str, float], None],
        on_failed: Callable[[str], None],
    ):
        self.ffmpeg = ffmpeg
        self.s = settings
        self._on_state = on_state
        self._on_saved = on_saved
        self._on_failed = on_failed

        self.buffer_dir = data_dir() / "buffer"
        self.state = STOPPED
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._feeder: Optional[audio.Feeder] = None
        self._capture: Optional[audio.AudioCapture] = None
        self._gen = 0                       # bumps on every start/stop, invalidates old threads
        self._stderr_tail: deque[str] = deque(maxlen=30)

        self.encoders: dict[str, bool] = {}
        self.grabber_dd = False
        self._probed = threading.Event()
        threading.Thread(target=self._probe, daemon=True, name="sk-probe").start()

    # ------------------------------------------------------------------ probing
    def _probe(self) -> None:
        try:
            self.grabber_dd = fu.has_filter(self.ffmpeg, "ddagrab")
            self.encoders = fu.detect_encoders(self.ffmpeg)
        finally:
            self._probed.set()

    def wait_probe(self, timeout: float = 30) -> bool:
        return self._probed.wait(timeout)

    # ------------------------------------------------------------------ state
    def _set_state(self, state: str, msg: str = "") -> None:
        self.state = state
        try:
            self._on_state(state, msg)
        except Exception:
            pass

    def is_running(self) -> bool:
        return self.state in (STARTING, RECORDING)

    def buffered_seconds(self) -> float:
        if not self.is_running():
            return 0.0
        n = len(fu.list_segments(self.buffer_dir))
        return float(min(n * fu.SEG_TIME, self.s.buffer_minutes * 60))

    # ------------------------------------------------------------------ start / stop
    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._gen += 1
            gen = self._gen
            self._set_state(STARTING, "Starting...")
        threading.Thread(target=self._start_worker, args=(gen,), daemon=True, name="sk-start").start()

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._teardown()
        fu.clear_segments(self.buffer_dir)
        self._set_state(STOPPED, "Stopped")

    def restart(self) -> None:
        self.stop()
        self.start()

    def _teardown(self) -> None:
        if self._feeder:
            self._feeder.stop()
            self._feeder = None
        proc, self._proc = self._proc, None
        if proc and proc.stdin:
            try:
                proc.stdin.close()      # unblocks ffmpeg if it is waiting for audio
            except Exception:
                pass
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._capture:
            self._capture.stop()
            self._capture = None

    def _spawn(self, encoder: str, grabber: str, with_audio: bool) -> subprocess.Popen:
        cmd = fu.build_capture_command(self.ffmpeg, self.s, encoder, grabber, with_audio, self.buffer_dir)
        self._stderr_tail.clear()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if with_audio else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=fu.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _start_worker(self, gen: int) -> None:
        self.wait_probe()
        with self._lock:
            if gen != self._gen:
                return
            s = self.s
            fu.clear_segments(self.buffer_dir)

            # audio
            warn = ""
            with_audio = bool(s.system_audio or s.mic_audio)
            cap = None
            if with_audio:
                try:
                    cap = audio.AudioCapture(
                        s.system_audio, s.system_device, s.system_volume,
                        s.mic_audio, s.mic_device, s.mic_volume,
                    )
                    cap.start()
                except Exception as e:
                    cap = None
                    with_audio = False
                    warn = f"Audio unavailable ({e}); recording video only."
            self._capture = cap

            # attempts: preferred encoder -> cpu -> gdigrab fallback
            enc = fu.resolve_encoder(s.encoder, self.encoders or {"cpu": True})
            dd = self.grabber_dd
            attempts = [(enc, "ddagrab" if dd else "gdigrab")]
            if dd and enc != "cpu":
                attempts.append(("cpu", "ddagrab"))
            if dd:
                attempts.append(("cpu", "gdigrab"))

            last_err = "FFmpeg stopped unexpectedly."
            for encoder, grabber in attempts:
                try:
                    proc = self._spawn(encoder, grabber, with_audio)
                except OSError as e:
                    last_err = str(e)
                    continue
                self._proc = proc
                if with_audio:
                    self._feeder = audio.Feeder(cap, proc.stdin)
                    self._feeder.start()
                # give ffmpeg a moment to prove it can run
                t_end = time.time() + 2.5
                while time.time() < t_end and proc.poll() is None:
                    time.sleep(0.1)
                if proc.poll() is None:
                    threading.Thread(target=self._monitor, args=(proc, gen), daemon=True).start()
                    threading.Thread(target=self._pruner, args=(gen,), daemon=True).start()
                    self._set_state(RECORDING, warn or f"Recording ({fu.ENCODER_NAMES[encoder]})")
                    return
                last_err = " | ".join(list(self._stderr_tail)[-3:]) or last_err
                if self._feeder:
                    self._feeder.stop()
                    self._feeder = None
                self._proc = None

            self._teardown()
            self._set_state(ERROR, last_err)

    def _monitor(self, proc: subprocess.Popen, gen: int) -> None:
        proc.wait()
        with self._lock:
            if gen != self._gen:
                return
            err = " | ".join(list(self._stderr_tail)[-3:]) or "Capture process stopped."
            self._teardown()
        self._set_state(ERROR, err)

    def _pruner(self, gen: int) -> None:
        keep = int(self.s.buffer_minutes * 60 / fu.SEG_TIME) + 3
        while True:
            time.sleep(3)
            if gen != self._gen:
                return
            segs = fu.list_segments(self.buffer_dir)
            for p in segs[:-keep]:
                try:
                    p.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------ saving clips
    def save_clip(self, seconds: Optional[int] = None) -> None:
        if not self.is_running():
            self._on_failed("The replay buffer is not running.")
            return
        secs = int(seconds or self.s.clip_seconds)
        secs = max(5, min(secs, self.s.buffer_minutes * 60))
        segs = fu.list_segments(self.buffer_dir)  # snapshot at the moment of the button press
        threading.Thread(target=self._save_worker, args=(segs, secs), daemon=True, name="sk-save").start()

    def _save_worker(self, segs: list[Path], secs: int) -> None:
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            out = fu.unique_path(Path(self.s.output_dir), f"{self.s.filename_prefix} {stamp}")
            dur = fu.assemble_clip(self.ffmpeg, segs, secs, out)
            self._on_saved(str(out), dur)
        except Exception as e:
            self._on_failed(str(e))
