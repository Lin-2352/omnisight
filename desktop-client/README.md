# desktop-client

The native Windows HUD. `Alt+C` captures the active monitor and analyzes it. `Alt+V` is push-to-talk: it captures your voice and the screen together.

| Path | Purpose | Phase |
| --- | --- | --- |
| `requirements-windows.txt` | Pinned dependencies (Windows x64, Python 3.10–3.13) | 1 |
| `capture/screen.py` | DPI-aware multi-monitor capture, then LANCZOS resize, then JPEG/Base64 | 3 |
| `capture/audio.py` | Push-to-talk ring buffer (sounddevice) | 3 |
| `network/client.py` | Thread-safe client with 30 s cached gist discovery | 3 |
| `ui/hud.py` | Frameless, translucent PyQt6 overlay | 3 |
| `main.py` | Global hotkeys and single-instance lock | 3 |

The client only **reads** the public gist, so it never needs a GitHub token.
