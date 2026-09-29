"""Static audit: no credentials in the repository, its history, or the web build.

The gist is public, the GitHub repo is public and the Vercel site serves its client
bundles to anyone, so a leaked GitHub token, Gemini key, Hugging Face token or
cloudflared credential would be exposed immediately. This scans:

* every tracked or new (non-ignored) file, plus ``kaggle-server/``, ``desktop-client/``
  and ``web-showcase/src/`` explicitly;
* the full git history (a key that was committed and later deleted is still public);
* ``web-showcase/.next/static`` (what browsers download) when a build exists.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.support import REPO_ROOT

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "github classic token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    "github fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    "google api key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "hugging face token": re.compile(r"\bhf_[A-Za-z0-9]{34}\b"),
    "anthropic/openai key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{32,}\b"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Built from pieces so this file does not match its own detector.
    "cloudflared credentials": re.compile(re.escape('"Tunnel' + 'Secret"') + r"\s*:|" + re.escape("-----BEGIN " + "ARGO TUNNEL TOKEN-----")),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}

BINARY_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".ico", ".gif", ".woff", ".woff2", ".ttf", ".pyc", ".wav", ".zip", ".gz"}
SECRET_ENV_KEYS = ("GITHUB_TOKEN", "OMNISIGHT_CLIENT_GITHUB_TOKEN", "OMNISIGHT_API_KEY", "GEMINI_API_KEY", "HF_TOKEN")


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if result.returncode != 0:
        pytest.skip(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def scan(text: str) -> list[str]:
    return [f"{name}: {match.group(0)[:12]}..." for name, pattern in SECRET_PATTERNS.items() for match in pattern.finditer(text)]


def candidate_files() -> list[Path]:
    listed = {REPO_ROOT / line for line in git("ls-files", "-co", "--exclude-standard").splitlines() if line}
    for folder in ("kaggle-server", "desktop-client", "web-showcase/src"):
        listed.update(p for p in (REPO_ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return sorted(p for p in listed if p.is_file() and p.suffix.lower() not in BINARY_SUFFIXES)


def test_patterns_detect_realistic_fakes() -> None:
    fakes = {
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8": "github classic token",
        "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q": "google api key",
        "hf_" + "AbCdEfGhIjKlMnOpQrStUvWxYz01234567": "hugging face token",
        '{"AccountTag": "x", "Tunnel' + 'Secret": "y"}': "cloudflared credentials",
    }
    for text, expected in fakes.items():
        assert any(hit.startswith(expected) for hit in scan(text)), text


def test_no_secrets_in_the_working_tree() -> None:
    files = candidate_files()
    assert len(files) > 50, "the scan looked at suspiciously few files"
    findings = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings += [f"{path.relative_to(REPO_ROOT)}: {hit}" for hit in scan(text)]
    assert findings == []


def test_no_secrets_anywhere_in_git_history() -> None:
    history = git("log", "-p", "--all", "--no-color", "--no-ext-diff")
    assert history.strip(), "empty history"
    assert scan(history) == []


def test_env_files_are_ignored_and_the_template_holds_no_values() -> None:
    for name in (".env", ".env.local", "web-showcase/.env.local", "kaggle-server/.env", "desktop-client/.env"):
        result = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO_ROOT, check=False)
        assert result.returncode == 0, f"{name} is not git-ignored"
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in SECRET_ENV_KEYS:
        for line in template.splitlines():
            if line.startswith(f"{key}="):
                assert line == f"{key}=", f".env.example ships a value for {key}"


def test_the_public_gist_record_schema_cannot_hold_credentials() -> None:
    from omnisight_contracts import EndpointRecord

    assert set(EndpointRecord.model_fields) == {"omnisight_endpoint", "model", "status", "updated_at", "gpu_device"}


def test_browser_bundles_hold_no_keys_or_server_env_names() -> None:
    static = REPO_ROOT / "web-showcase" / ".next" / "static"
    if not static.is_dir():
        pytest.skip("no web build (run `npm run build` in web-showcase)")
    findings = []
    for path in static.rglob("*.js"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        findings += [f"{path.name}: {hit}" for hit in scan(text)]
        for name in ("GEMINI_API_KEY", "GITHUB_TOKEN", "x-goog-api-key", "generativelanguage.googleapis.com"):
            if name in text:
                findings.append(f"{path.name}: server-only reference {name!r} in a client bundle")
    assert findings == []
