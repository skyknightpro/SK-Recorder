# SK Recorder 1.1

Always-on replay buffer for your screen. It keeps recording quietly in the background; press a hotkey (default **Alt+F10**) and the last 30 s / 1 min / 2 min / 5 min / any custom length is saved as an MP4 with video + game sound (+ optional microphone).

## Build the .exe (Windows)
1. Install Python 3.12 (`winget install Python.Python.3.12`). Python 3.14+ is too new for the audio library.
2. Double-click **build.bat**. It reuses (or creates) `.venv`, installs everything, downloads FFmpeg, and produces `dist\SK Recorder.exe`.
3. That single exe is self-contained (FFmpeg is bundled) - share it as is.

No Windows PC? Push this folder to GitHub, then publish a Release - the exe is built and attached automatically.

## Use
- Slide the **Replay buffer** switch on. Pick the clip length, press the hotkey or the big button.
- **Settings**: screen, fps, quality, resolution, GPU encoder (NVIDIA/AMD/Intel), system sound + microphone with volumes, audio sync offset, save folder, hotkey, start with Windows.
- Closing the window keeps it running in the tray; right-click the tray icon to quit.

## Stability & performance
- **The UI never waits for FFmpeg.** Starting, stopping and restarting the capture happen on worker threads, so the window, the tray and the hotkey stay responsive even when the encoder is busy.
- **Automatic recovery.** If Windows or FFmpeg stops the capture, SK Recorder restarts it with backoff (3 s, 6 s, 12 s, 24 s, 30 s). It gives up after 5 attempts and shows the reason. You can turn this off in Settings.
- **Game-first scheduling.** The capture/encoder process runs at below-normal priority; the UI process stays at normal priority. With NVIDIA/AMD the frames go straight from the GPU into the hardware encoder ("GPU direct" - no RAM copy, almost no CPU).
- **No keyboard hook.** The hotkey uses the Windows `RegisterHotKey` API, so no key is inspected while you play.
- **Bounded audio.** Audio buffers are capped at two seconds per source; if the encoder stalls, old audio is dropped instead of growing memory until the app hangs.
- **Clean shutdown.** Capture processes are tied to the app with a Windows job object, so a crash or Task Manager kill cannot leave hidden `ffmpeg.exe` processes writing to disk.
- **Crash diagnostics.** Unhandled errors and native crashes are written to `%LOCALAPPDATA%\SKRecorder\crash.log`; normal activity goes to `log.txt` (Settings > Open log file).
- **Safe clip writing.** Clips are written to a temporary `.part.mp4` and renamed only when complete. Saves are single-flight; the rolling buffer respects the staging step, so a save never reads a deleted file.

## Notes
- Clips are cut without re-encoding, on segment (keyframe) boundaries. A normal clip is the length you chose plus up to 2 s; if the buffer has not filled up yet, the clip is as long as the footage that exists. The newest moment is always included.
- Sound is captured with WASAPI loopback: it records what you hear, per the chosen output device.
- Some exclusive-fullscreen games block screen capture; use borderless/windowed-fullscreen if the clip is black.
- Keep **Resolution = Original** for the lightest load; downscaling forces the heavier CPU path.
- Run from source: `pip install -r requirements.txt` then `python main.py` (add `--minimized` to start in the tray).
