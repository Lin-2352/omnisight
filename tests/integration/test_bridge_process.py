"""The real client started with ``--bridge``: token on stdin, port on stdout, a socket speaking the protocol, exit with the app."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "desktop-client" / "main.py"
TOKEN = "integration-token"


def _start() -> subprocess.Popen[str]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUNBUFFERED": "1", "FALLBACK_API_URL": "off", "OMNISIGHT_BACKEND": "local"}
    return subprocess.Popen(
        [sys.executable, "-u", str(MAIN), "--bridge", "--no-hotkeys", "--backend", "local"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(ROOT),
    )


def _read_lines(sock: socket.socket, want, timeout: float = 20.0) -> list[dict]:
    sock.settimeout(0.5)
    events: list[dict] = []
    buffer = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
        except TimeoutError:
            pass
        except OSError:
            break
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            events.append(json.loads(line))
        if want(events):
            return events
    return events


@pytest.mark.skipif(os.name != "nt", reason="the Windows client")
def test_bridge_mode_end_to_end() -> None:
    proc = _start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(TOKEN + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        announce = ""
        while time.monotonic() < deadline:
            announce = proc.stdout.readline()
            if announce.startswith("OMNISIGHT_BRIDGE") or proc.poll() is not None:
                break
        if not announce.startswith("OMNISIGHT_BRIDGE"):
            if proc.poll() == 0:
                pytest.skip("another OmniSight client holds the single-instance lock")
            pytest.fail(f"no announcement; exit={proc.poll()} stderr={proc.stderr.read() if proc.stderr else ''}")
        port = json.loads(announce.split(" ", 1)[1])["port"]

        # a wrong token is refused
        with socket.create_connection(("127.0.0.1", port), timeout=5) as bad:
            bad.sendall(json.dumps({"cmd": "auth", "token": "wrong"}).encode() + b"\n")
            assert _read_lines(bad, lambda e: bool(e), timeout=3) == []

        with socket.create_connection(("127.0.0.1", port), timeout=5) as good:
            good.sendall(json.dumps({"cmd": "auth", "token": TOKEN, "pid": os.getpid()}).encode() + b"\n")
            events = _read_lines(good, lambda e: {"hello", "engine", "node"} <= {x["event"] for x in e})
            kinds = {e["event"] for e in events}
            assert {"hello", "engine", "node", "switch"} <= kinds, events
            assert next(e for e in events if e["event"] == "engine")["key"].startswith("local")
            memory = next(e for e in events if e["event"] == "switch" and e["name"] == "memory")
            assert isinstance(memory["on"], bool)
            good.sendall(b'{"cmd":"ping"}\n')
            assert any(e["event"] == "pong" for e in _read_lines(good, lambda e: any(x["event"] == "pong" for x in e), timeout=10))
            good.sendall(b'{"cmd":"set","name":"actions","on":"yes"}\n')
            assert any(e["event"] == "error" for e in _read_lines(good, lambda e: any(x["event"] == "error" for x in e), timeout=10))
            # the client going away ends the process: the C# app owns it
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()
