# SK Recorder 1.0

Always-on replay buffer for your screen. It keeps recording quietly in the background; press a hotkey (default **Alt+F10**) and the last 30 s / 1 min / 2 min / 5 min / any custom length is saved as an MP4 with video + game sound (+ optional microphone).

## Build the .exe (Windows)
1. Install Python 3.12 (`winget install Python.Python.3.12`). Python 3.14+ is too new for the audio library.
2. Double-click **build.bat**. It installs everything, downloads FFmpeg, and produces `dist\SK Recorder.exe`.
3. That single exe is self-contained (FFmpeg is bundled) - share it as is.

No Windows PC? Push this folder to GitHub and run the **Build SK Recorder** workflow (Actions tab); download the exe from the run's artifacts.

## Use
- Slide the **Replay buffer** switch on. Pick the clip length, press the hotkey or the big button.
- **Settings**: screen, fps, quality, resolution, GPU encoder (NVIDIA/AMD/Intel), system sound + microphone with volumes, audio sync offset, save folder, hotkey, start with Windows.
- Closing the window keeps it running in the tray; right-click the tray icon to quit.

## Notes
- Clips are cut without re-encoding, so a clip is the length you chose plus up to 2 s (it starts on a keyframe).
- Sound is captured with WASAPI loopback: it records what you hear, per the chosen output device.
- Some exclusive-fullscreen games block screen capture; use borderless/windowed-fullscreen if the clip is black.
- Run from source: `pip install -r requirements.txt` then `python main.py`.
