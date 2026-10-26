"""Replay-buffer engine: keeps recording the screen into rolling segments, saves clips on demand.

This module has no Qt dependency. The UI passes plain callbacks.

Control operations are deliberately non-blocking: the UI thread never waits for
FFmpeg to start, stop or probe. Long work happens on short-lived worker threads,
and every operation is tagged with a generation number so stale workers cannot
revive an old recording.
"""
from __future__ import annotations

import shutil
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

STOPPED, STARTING, RECORDING, STOPPING, ERROR = "stopped", "starting", "recording", "stopping", "error"

MAX_RECOVERY_ATTEMPTS = 5


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
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._cleanup_done = threading.Event()
        self._cleanup_done.set()
        self._snapshot_ready = threading.Event()
        self._snapshot_ready.set()
        self._save_lock = threading.Lock()
        self._saving = False
        self._shutdown = threading.Event()
        self._shutdown_done = False
        self._last_record_t = 0.0
        self._recover_attempts = 0

        self.encoders: dict[str, bool] = {}
        self.grabber_dd = False
        self.zerocopy: dict[str, bool] = {}
        self._probed = threading.Event()
        threading.Thread(target=self._probe, daemon=True, name="sk-probe").start()

    # ------------------------------------------------------------------ probing
    def _probe(self) -> None:
        try:
            self.grabber_dd = fu.has_filter(self.ffmpeg, "ddagrab")
            self.encoders = fu.detect_encoders(self.ffmpeg)
            if self.grabber_dd:
                self.zerocopy = {
                    k: bool(self.encoders.get(k)) and fu.test_zero_copy(self.ffmpeg, k)
                    for k in ("nvenc", "amf")
                }
            fu.log(f"probe: ddagrab={self.grabber_dd} encoders={self.encoders} zerocopy={self.zerocopy}")
        except Exception as e:
            fu.log_exception("probe failed", e)
        finally:
            self._probed.set()

    def wait_probe(self, timeout: float = 45) -> bool:
        return self._probed.wait(timeout)

    # ------------------------------------------------------------------ state
    def _set_state(self, state: str, msg: str = "") -> None:
        self.state = state
        try:
            self._on_state(state, msg)
        except Exception as e:
            fu.log_exception("state callback failed", e)

    def is_running(self) -> bool:
        return self.state in (STARTING, RECORDING)

    def buffered_seconds(self) -> float:
        if not self.is_running():
            return 0.0
        n = len(fu.list_segments(self.buffer_dir))
        return float(min(n * fu.SEG_TIME, self.s.buffer_minutes * 60))

    # ------------------------------------------------------------------ start / stop
    def start(self) -> None:
        self._begin_start(reset_recovery=True)

    def _begin_start(self, reset_recovery: bool) -> None:
        with self._lock:
            if self._shutdown.is_set() or self.state in (STARTING, RECORDING):
                return
            if reset_recovery:
                self._recover_attempts = 0
            self._gen += 1
            gen = self._gen
        self._set_state(STARTING, "Starting...")
        threading.Thread(target=self._start_worker, args=(gen,), daemon=True, name="sk-start").start()

    def stop(self) -> None:
        """Return immediately; teardown runs on a worker thread."""
        with self._lock:
            if self.state == STOPPING:
                return
            if self.state == STOPPED and self._cleanup_done.is_set() and not self._has_refs():
                return
            self._gen += 1
            gen = self._gen
            refs = self._take_refs_locked()
            self._cleanup_done.clear()
        self._set_state(STOPPING, "Stopping...")
        threading.Thread(target=self._stop_worker, args=(gen, refs), daemon=True, name="sk-stop").start()

    def restart(self) -> None:
        self.stop()
        self.start()

    def _has_refs(self) -> bool:
        return bool(self._proc or self._feeder or self._capture)

    def _take_refs_locked(self):
        proc, self._proc = self._proc, None
        feeder, self._feeder = self._feeder, None
        cap, self._capture = self._capture, None
        return proc, feeder, cap

    def _stop_worker(self, gen: int, refs) -> None:
        try:
            self._cleanup_refs(refs)
            # A clip that is still being staged may be reading the originals.
            if self._saving and not self._snapshot_ready.is_set():
                self._snapshot_ready.wait(15)
            if not self._saving or self._snapshot_ready.is_set():
                fu.clear_segments(self.buffer_dir)
        except Exception as e:
            fu.log_exception("stop failed", e)
        finally:
            self._cleanup_done.set()
            if gen == self._gen and not self._shutdown.is_set():
                self._set_state(STOPPED, "Stopped")

    def _cleanup_refs(self, refs, terminate_timeout: float = 1.2) -> None:
        proc, feeder, cap = refs
        if feeder is not None:
            feeder.stop()
            try:
                feeder.join(0.25)
            except Exception:
                pass
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()      # unblocks ffmpeg if it is waiting for audio
            except Exception:
                pass
            if feeder is not None:
                try:
                    feeder.join(0.5)
                except Exception:
                    pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=terminate_timeout)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
        if cap is not None:
            try:
                cap.stop()
            except Exception as e:
                fu.log_exception("audio stop failed", e)

    def _spawn(self, encoder: str, grabber: str, with_audio: bool, mode: str) -> subprocess.Popen:
        cmd = fu.build_capture_command(self.ffmpeg, self.s, encoder, grabber, with_audio, self.buffer_dir, mode)
        fu.log("start: " + " ".join(cmd))
        self._stderr_tail.clear()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if with_audio else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=fu.CAPTURE_FLAGS,
        )
        fu.attach_job(proc)
        threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True, name="sk-stderr").start()
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _last_error(self) -> str:
        """Pick a useful FFmpeg message, ignoring harmless startup notices."""
        ignore = ("guessed channel layout", "deprecated", "not enough frames to estimate rate",
                  "consider increasing probesize")
        found: list[str] = []
        for line in reversed(self._stderr_tail):
            low = line.lower()
            if any(x in low for x in ignore):
                continue
            if any(x in low for x in ("error", "failed", "unable", "cannot", "could not",
                                      "invalid", "no such", "not found", "denied")):
                found.append(line)
                if len(found) >= 2:
                    break
        if found:
            return " | ".join(reversed(found))
        return "The capture process stopped unexpectedly."

    def _start_worker(self, gen: int) -> None:
        try:
            if not self._cleanup_done.wait(10):
                self._fail_start(gen, "The previous capture did not stop cleanly. Please try again.")
                return
            self.wait_probe(45)
            with self._lock:
                if gen != self._gen or self._shutdown.is_set():
                    return
            s = self.s
            try:
                self.buffer_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._fail_start(gen, f"Cannot create the replay buffer folder: {e}")
                return
            if not self._snapshot_ready.wait(30):
                self._fail_start(gen, "A clip save is still being prepared. Please try again.")
                return
            with self._lock:
                if gen != self._gen or self._shutdown.is_set():
                    return
            fu.clear_segments(self.buffer_dir)

            disk_error = self._check_free_space()
            if disk_error:
                self._fail_start(gen, disk_error)
                return

            # --- audio -------------------------------------------------
            warnings: list[str] = []
            with_audio = bool(s.system_audio or s.mic_audio)
            cap: Optional[audio.AudioCapture] = None
            if with_audio:
                try:
                    cap = audio.AudioCapture(
                        s.system_audio, s.system_device, s.system_volume,
                        s.mic_audio, s.mic_device, s.mic_volume,
                    )
                    cap.start()
                    warnings.extend(cap.warnings)
                except Exception as e:
                    cap = None
                    with_audio = False
                    warnings.append(f"Audio unavailable ({e}); recording video only.")
                    fu.log(f"audio unavailable: {e}")
                if cap is not None:
                    with self._lock:
                        stale = gen != self._gen or self._shutdown.is_set()
                        if not stale:
                            self._capture = cap
                    if stale:
                        try:
                            cap.stop()
                        except Exception:
                            pass
                        return

            # attempts, lightest first: GPU-direct -> GPU with RAM copy -> CPU encoder
            enc = fu.resolve_encoder(s.encoder, self.encoders or {"cpu": True})
            dd = self.grabber_dd
            grab = "ddagrab" if dd else "gdigrab"
            attempts: list[tuple[str, str, str]] = []
            if dd and s.resolution == 0 and self.zerocopy.get(enc):
                attempts.append((enc, "ddagrab", "zerocopy"))
            attempts.append((enc, grab, "download"))
            if enc != "cpu":
                attempts.append(("cpu", grab, "download"))
            if dd:
                attempts.append(("cpu", "gdigrab", "download"))
            # remove duplicates while preserving order
            seen = set()
            attempts = [a for a in attempts if not (a in seen or seen.add(a))]

            last_err = "FFmpeg stopped unexpectedly."
            audio_passes = [True, False] if (with_audio and cap is not None) else [False]
            for pass_idx, use_audio in enumerate(audio_passes):
                if pass_idx == 1:
                    warnings.append("Audio could not be used; recording video only.")
                    fu.log("all audio attempts failed; retrying without audio")
                for encoder, grabber, mode in attempts:
                    with self._lock:
                        if gen != self._gen or self._shutdown.is_set():
                            return
                    try:
                        proc = self._spawn(encoder, grabber, use_audio, mode)
                    except OSError as e:
                        last_err = str(e)
                        continue

                    feeder = None
                    if use_audio and cap is not None:
                        try:
                            feeder = audio.Feeder(cap, proc.stdin)
                            feeder.start()
                        except Exception as e:
                            last_err = str(e)
                            self._cleanup_refs((proc, None, None), terminate_timeout=0.5)
                            continue

                    with self._lock:
                        stale = gen != self._gen or self._shutdown.is_set()
                        if not stale:
                            self._proc = proc
                            self._feeder = feeder
                    if stale:
                        self._cleanup_refs((proc, feeder, None), terminate_timeout=0.5)
                        return

                    if self._wait_ready(proc):
                        with self._lock:
                            if gen != self._gen or self._shutdown.is_set():
                                return
                        self._last_record_t = time.time()
                        threading.Thread(target=self._monitor, args=(proc, gen), daemon=True, name="sk-monitor").start()
                        threading.Thread(target=self._watchdog, args=(proc, gen), daemon=True, name="sk-watchdog").start()
                        threading.Thread(target=self._pruner, args=(gen,), daemon=True, name="sk-prune").start()
                        label = "GPU direct" if mode == "zerocopy" else "standard"
                        notes = list(warnings)
                        if encoder != enc:
                            notes.append(
                                f"GPU encoder unavailable - using CPU ({fu.ENCODER_NAMES[encoder]}). "
                                "Expect higher CPU load."
                            )
                        msg = "; ".join(notes) if notes else f"Recording ({fu.ENCODER_NAMES[encoder]}, {label})"
                        fu.log(f"recording: {fu.ENCODER_NAMES[encoder]} / {grabber} / {mode} / audio={use_audio}")
                        self._set_state(RECORDING, msg)
                        return

                    last_err = self._last_error()
                    fu.log(f"attempt failed ({encoder}/{grabber}/{mode}/audio={use_audio}): {last_err}")
                    with self._lock:
                        if self._proc is proc:
                            self._proc = None
                        if self._feeder is feeder:
                            self._feeder = None
                    self._cleanup_refs((proc, feeder, None), terminate_timeout=0.5)

            self._fail_start(gen, last_err)
        except Exception as e:
            fu.log_exception("start failed", e)
            self._fail_start(gen, str(e))

    def _fail_start(self, gen: int, msg: str) -> None:
        with self._lock:
            if gen != self._gen or self._shutdown.is_set():
                return
            refs = self._take_refs_locked()
            self._cleanup_done.clear()
        try:
            self._cleanup_refs(refs)
        finally:
            self._cleanup_done.set()
        with self._lock:
            if gen != self._gen:
                return
        self._set_state(ERROR, msg)
        self._maybe_recover(gen)

    def _wait_ready(self, proc: subprocess.Popen, timeout: float = 2.2) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if proc.poll() is not None:
                return False
            time.sleep(0.05)
        return proc.poll() is None

    def _check_free_space(self) -> str:
        try:
            free = shutil.disk_usage(str(self.buffer_dir)).free
        except OSError:
            return ""
        need = (self.s.estimated_buffer_mb() + 128) * 1024 * 1024
        if free < need:
            return (
                "Not enough free disk space for the replay buffer "
                f"({free // (1024 * 1024)} MB free, about {need // (1024 * 1024)} MB needed). "
                "Lower the buffer length/quality or free some space."
            )
        return ""

    # ------------------------------------------------------------------ monitoring / recovery
    def _monitor(self, proc: subprocess.Popen, gen: int) -> None:
        try:
            proc.wait()
        except Exception:
            pass
        with self._lock:
            if gen != self._gen:
                return
            self._gen += 1
            active_gen = self._gen
            self._cleanup_done.clear()
            refs = self._take_refs_locked()
        err = self._last_error()
        uptime = time.time() - self._last_record_t
        if uptime >= 30:
            self._recover_attempts = 0
        fu.log(f"capture stopped after {uptime:.1f}s: {err}")
        self._set_state(ERROR, err)
        try:
            self._cleanup_refs(refs, terminate_timeout=0.5)
        finally:
            self._cleanup_done.set()
        self._maybe_recover(active_gen)

    def _maybe_recover(self, gen: int) -> None:
        if self._shutdown.is_set() or not self.s.auto_recover:
            return
        with self._lock:
            if gen != self._gen or self.state != ERROR:
                return
            self._recover_attempts += 1
            attempt = self._recover_attempts
        if attempt > MAX_RECOVERY_ATTEMPTS:
            fu.log("automatic recovery gave up after several failed attempts")
            return
        delay = min(30, 3 * (2 ** (attempt - 1)))
        fu.log(f"automatic recovery attempt {attempt} in {delay}s")

        def worker() -> None:
            if self._shutdown.wait(delay):
                return
            with self._lock:
                if gen != self._gen or self.state != ERROR:
                    return
            self._begin_start(reset_recovery=False)

        threading.Thread(target=worker, daemon=True, name="sk-recover").start()

    def _pruner(self, gen: int) -> None:
        while not self._shutdown.is_set():
            if self._shutdown.wait(3):
                return
            try:
                with self._save_lock:
                    if self._saving:
                        continue
                    with self._lock:
                        if gen != self._gen:
                            return
                        keep = max(3, int(self.s.buffer_minutes * 60 / fu.SEG_TIME) + 3)
                    segs = fu.list_segments(self.buffer_dir)
                    for p in segs[:-keep]:
                        try:
                            p.unlink()
                        except OSError:
                            pass
            except Exception as e:
                fu.log_exception("pruner failed", e)

    def _watchdog(self, proc: subprocess.Popen, gen: int) -> None:
        """Kill a capture process that is alive but no longer writing segments.

        A hung encoder would otherwise look like a healthy recording while the
        buffer quietly stops growing. Killing the process makes the normal
        monitor/recovery path take over.
        """
        last_size = -1
        stalled = 0
        while not self._shutdown.is_set():
            if self._shutdown.wait(2.0):
                return
            with self._lock:
                if gen != self._gen or self._proc is not proc:
                    return
            if proc.poll() is not None:
                return
            try:
                segs = fu.list_segments(self.buffer_dir)
                size = sum(p.stat().st_size for p in segs[-2:]) if segs else 0
            except OSError:
                continue
            if size == last_size:
                stalled += 2
                if stalled >= 12:
                    fu.log("watchdog: capture stalled for 12s; restarting the encoder")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
            else:
                stalled = 0
                last_size = size

    # ------------------------------------------------------------------ saving clips
    def save_clip(self, seconds: Optional[int] = None) -> None:
        if not self.is_running():
            self._on_failed("The replay buffer is not running.")
            return
        rejected = False
        with self._save_lock:
            if self._saving:
                rejected = True
                segs = []
                secs = 0
            else:
                self._saving = True
                self._snapshot_ready.clear()
                secs = int(seconds or self.s.clip_seconds)
                secs = max(5, min(secs, self.s.buffer_minutes * 60))
                segs = fu.list_segments(self.buffer_dir)  # snapshot at the moment of the button press
        if rejected:
            self._on_failed("A clip is already being saved. Please wait for it to finish.")
            return
        if not segs:
            with self._save_lock:
                self._saving = False
                self._snapshot_ready.set()
            self._on_failed("The replay buffer is still starting - try again in a couple of seconds.")
            return
        threading.Thread(target=self._save_worker, args=(segs, secs), daemon=True, name="sk-save").start()

    def _save_worker(self, segs: list[Path], secs: int) -> None:
        out: Optional[Path] = None
        dur = 0.0
        error: Optional[str] = None
        try:
            out_dir = Path(self.s.output_dir).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            prefix = fu.sanitize_filename(self.s.filename_prefix, "SK Clip")
            out = fu.unique_path(out_dir, f"{prefix} {stamp}")
            fu.log(f"saving {secs}s clip -> {out}")
            dur = fu.assemble_clip(
                self.ffmpeg, segs, secs, out, on_staged=self._snapshot_ready.set,
            )
        except Exception as e:
            fu.log_exception("save failed", e)
            error = str(e)
        finally:
            self._snapshot_ready.set()
            with self._save_lock:
                self._saving = False
        # Callbacks run after the "saving" flag is cleared, so a user can
        # immediately save again without hitting a spurious "already saving".
        if error is not None or out is None:
            self._on_failed(error or "Could not write the clip.")
        else:
            fu.log(f"clip saved: {out} ({dur:.1f}s)")
            self._on_saved(str(out), dur)

    # ------------------------------------------------------------------ shutdown
    def shutdown(self, timeout: float = 3.5) -> None:
        """Synchronous, bounded shutdown used when the app is really quitting."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._shutdown.set()
        with self._lock:
            self._gen += 1
            refs = self._take_refs_locked()
            self._cleanup_done.clear()
        self._set_state(STOPPING, "Stopping...")
        thread = threading.Thread(target=self._shutdown_worker, args=(refs,), daemon=True, name="sk-shutdown")
        thread.start()
        thread.join(timeout)
        self._set_state(STOPPED, "Stopped")

    def _shutdown_worker(self, refs) -> None:
        try:
            self._cleanup_refs(refs, terminate_timeout=0.8)
        except Exception as e:
            fu.log_exception("shutdown cleanup failed", e)
        finally:
            self._cleanup_done.set()
