# OmniSight

OmniSight looks at your screen and answers questions about it. See an error you do not understand? Ask by typing, with a hotkey, or by voice. It can remember the conversation, search the web, read answers aloud, watch for errors, and (only if you allow it) run a command after you approve it.

**New here? Read the [user guide](docs/USER_GUIDE.md).** It explains every feature in simple words, including what leaves your PC.

## Quick start (Windows 10/11, Python 3.10 to 3.13)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r desktop-client\requirements-windows.txt
python -m pip install -e .
python desktop-client\main.py
```

1. The OmniSight window opens (and an icon in the tray).
2. Type a question and press Enter, or press `Alt+C` anywhere to explain the screen.
3. Pick **This PC** in the Engine menu to keep every screenshot on your computer.

## A newer window (preview)

`dotnet run --project desktop-app\OmniSight.App` starts a modern Fluent-style window (WPF) on the same engine: answer cards, an options panel, an in-window approval for running commands, and light and dark themes. See [desktop-app/README.md](desktop-app/README.md).

## What is in this repository

| Folder | What it is |
| --- | --- |
| [`desktop-client/`](desktop-client/README.md) | The Windows engine and the original window: capture, hotkeys, tray, search, watch, safe commands |
| [`desktop-app/`](desktop-app/README.md) | The newer C# window (WPF) that drives the same engine |
| [`kaggle-server/`](kaggle-server/README.md) | The model server that runs on a free Kaggle GPU (also runs on your PC) |
| [`web-showcase/`](web-showcase/README.md) | The website and its online backup, hosted on Vercel |
| [`shared/`](shared) | The request and answer format all three parts agree on |
| [`tests/`](tests/README.md) | The automated tests |
| [`docs/`](docs/README.md) | The guides |

Releases are tagged: `v1-stable` is the first version, `v2-jarvis` adds memory, chat, voice, web search, watch mode and running commands.
