"""Running a command the model proposed, only after the user approved that exact text. Qt-free.

The model can be steered by text on the screen or in a web result, so the approval is the only gate and
everything here assumes the proposal is hostile:

* ``check_command`` refuses a short list of catastrophic or security-weakening commands even when the
  user approves them, and lists warnings for risky ones. It is a safety net, **not a sandbox**: a finite
  list cannot recognise every harmful command.
* ``Approval`` is built from the exact text shown to the user and what they typed (``RUN``). It carries the
  SHA-256 of that text, and ``ActionRunner.run`` refuses unless everything still matches, so a command
  swapped after the dialog was shown cannot run.
* The command runs as an argv list (never through a shell string we build), with stdin closed, no console
  window, a scrubbed environment, a timeout and output cap, inside a kill-on-close Job Object so a timeout,
  a Stop click or the app exiting ends the whole process tree.
* Every decision is written to an audit log (the command text, never its output). The output is shown to
  the user and goes nowhere else: it is not fed back to the model, so there is no autonomous loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol

from core.logger import get_logger
from core.node_supervisor import JobLike, default_job

logger = get_logger("actions")

CONFIRM_WORD: Final[str] = "RUN"
MAX_COMMAND_CHARS: Final[int] = 2000
MAX_COMMAND_LINES: Final[int] = 20
DEFAULT_TIMEOUT_S: Final[float] = 60.0
MAX_TIMEOUT_S: Final[float] = 300.0
MAX_OUTPUT_BYTES: Final[int] = 64 * 1024
AUDIT_MAX_BYTES: Final[int] = 1_000_000
AUDIT_BACKUPS: Final[int] = 3

#: Code-block language tags that mean "a command for a terminal".
CMD_LANGUAGES: Final[frozenset[str]] = frozenset({"cmd", "bat", "batch", "dos"})
SHELL_LANGUAGES: Final[frozenset[str]] = frozenset(
    {"bash", "sh", "shell", "zsh", "console", "terminal", "powershell", "pwsh", "ps1", "ps", "cmd", "bat", "batch", "dos"}
)


def is_shell_language(language: str) -> bool:
    return language.strip().lower() in SHELL_LANGUAGES


def shell_for(language: str) -> str:
    """``cmd`` for batch-style tags, PowerShell for everything else (git, python and npm work the same in both)."""
    return "cmd" if language.strip().lower() in CMD_LANGUAGES else "powershell"


# ---------------------------------------------------------------------------
# What may run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """``refused`` is a reason the command can never run; ``warnings`` are shown next to the command."""

    refused: str | None = None
    warnings: tuple[str, ...] = ()


_STRIP = re.compile(r"[`^\"']")
_SPACES = re.compile(r"\s+")
_SEGMENT = re.compile(r"&&|\|\||[;&|\n]")

_ALWAYS_REFUSED: Final[dict[str, str]] = {
    "format": "formats a disk",
    "format-volume": "formats a disk",
    "clear-disk": "erases a disk",
    "remove-partition": "deletes a partition",
    "initialize-disk": "initialises a disk",
    "diskpart": "changes disks and partitions",
    "bcdedit": "changes the boot configuration",
    "shutdown": "shuts the computer down",
    "logoff": "logs the user off",
    "restart-computer": "restarts the computer",
    "stop-computer": "shuts the computer down",
    "runas": "starts a program as another user",
    "sudo": "raises privileges",
    "vssadmin": "manages or deletes backups",
    "cipher": "can wipe free disk space",
    "cmdkey": "manages stored credentials",
    "mimikatz": "steals credentials",
    "set-executionpolicy": "changes a security setting",
    "set-mppreference": "changes Windows Defender",
    "add-mppreference": "changes Windows Defender",
    "clear-eventlog": "erases system logs",
    "wevtutil": "can erase system logs",
    "sdelete": "securely wipes data",
    "takeown": "takes ownership of files",
    "icacls": "changes file permissions",
    "cacls": "changes file permissions",
}
_DELETE_VERBS: Final[frozenset[str]] = frozenset({"rm", "ri", "del", "erase", "rd", "rmdir", "remove-item"})
_INSTALL_VERBS: Final[frozenset[str]] = frozenset({"pip", "pip3", "npm", "pnpm", "yarn", "winget", "choco", "scoop", "apt", "apt-get", "brew", "cargo", "gem"})
_DOWNLOAD_WORDS = re.compile(r"\b(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|downloadstring|downloadfile|bitsadmin|start-bitstransfer|certutil)\b")
_EXECUTE_WORDS = re.compile(r"(\|\s*(iex|invoke-expression|sh|bash|zsh|cmd|powershell|pwsh)\b|\b(iex|invoke-expression)\b)")
_BASE64 = re.compile(r"frombase64string|\[convert\]::frombase64")
_POWERSHELL_ENCODED = frozenset({"-e", "-ec", "-en", "-enc", "-enco", "-encod", "-encode", "-encoded", "-encodedc", "-encodedco", "-encodedcom", "-encodedcomm", "-encodedcomma", "-encodedcomman", "-encodedcommand"})
_NESTED_SHELLS = frozenset({"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "wsl", "start"})
_NESTED_FLAGS = frozenset({"-c", "-command", "-com", "-comm", "-comma", "-comman", "/c", "/k", "-lc", "-ic"})
_ELEVATE = re.compile(r"-verb\s+runas|\brunas\b")
_OWN_FILES = re.compile(r"omnisight[\\/].*(logs|history|settings|\.env)|%appdata%[\\/]omnisight|\$env:appdata[\\/]omnisight|history\.json|actions\.log")
_REG_WRITES = frozenset({"add", "delete", "import", "load", "unload", "restore", "save", "copy"})

#: Targets a recursive delete must never touch (matched on a normalised token).
_DANGEROUS_TARGETS = re.compile(
    r"^("
    r"[/\\]\*?|~[/\\]?\*?|\*|[a-z]:[/\\]?\*?|"
    r"(%userprofile%|%homepath%|%homedrive%%homepath%|\$home|\$env:userprofile|\$env:homepath)([/\\]\*?)?|"
    r"(%windir%|%systemroot%|%programfiles%|%programdata%|\$env:windir|\$env:systemroot)([/\\].*)?|"
    r"[a-z]:[/\\](windows|program files|program files \(x86\)|programdata)([/\\].*)?|"
    r"[a-z]:[/\\]users([/\\][^/\\]+)?([/\\]\*)?"
    r")$"
)


def _normalise(text: str) -> str:
    return _SPACES.sub(" ", _STRIP.sub("", text)).strip().lower()


def _tokens(segment: str) -> list[str]:
    try:
        parts = shlex.split(segment, posix=False)
    except ValueError:
        parts = segment.split()
    return [_STRIP.sub("", part).lower() for part in parts if part.strip()]


def _verb(tokens: list[str]) -> str:
    if not tokens:
        return ""
    name = tokens[0].replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".exe") else name


def _is_dangerous_delete(tokens: list[str]) -> bool:
    flags = [t for t in tokens[1:] if t.startswith(("-", "/")) and len(t) > 1 and not _DANGEROUS_TARGETS.match(t)]
    recursive = any(
        f in {"-r", "-rf", "-fr", "-recurse", "--recursive", "-force", "/s", "/q", "-f", "--force"} or re.fullmatch(r"-[a-z]*r[a-z]*", f) is not None
        for f in flags
    )
    targets = [t for t in tokens[1:] if t not in flags]
    return recursive and any(_DANGEROUS_TARGETS.match(t) for t in targets)


def _inner_command(segment: str, tokens: list[str]) -> str:
    """The command a nested shell (``powershell -Command ...``, ``cmd /c ...``, ``bash -c ...``) will run."""
    for index, token in enumerate(tokens[1:], start=1):
        if token in _NESTED_FLAGS:
            lowered = segment.lower()
            position = lowered.find(token, lowered.find(tokens[0]) + len(tokens[0]))
            return segment[position + len(token):].strip().strip("\"'") if position >= 0 else " ".join(tokens[index + 1:])
    return ""


def check_command(text: str) -> Verdict:
    """Classify ``text``: refuse what must never run, warn about what deserves a second look."""
    if not text.strip():
        return Verdict(refused="The command is empty.")
    if len(text) > MAX_COMMAND_CHARS:
        return Verdict(refused=f"The command is {len(text)} characters long; the limit is {MAX_COMMAND_CHARS}.")
    if len([line for line in text.splitlines() if line.strip()]) > MAX_COMMAND_LINES:
        return Verdict(refused=f"The command has more than {MAX_COMMAND_LINES} lines; run long scripts yourself.")
    if "\x00" in text:
        return Verdict(refused="The command contains a control character.")

    flat = _normalise(text)
    if _BASE64.search(flat):
        return Verdict(refused="It decodes a hidden string.")
    if _ELEVATE.search(flat):
        return Verdict(refused="It asks for administrator rights.")
    if _DOWNLOAD_WORDS.search(flat) and _EXECUTE_WORDS.search(flat):
        return Verdict(refused="It downloads something and runs it.")
    if re.search(r"\|\s*(iex|invoke-expression|sh|bash|zsh|powershell|pwsh)\b", flat):
        return Verdict(refused="It pipes text into a shell or Invoke-Expression.")
    if _OWN_FILES.search(flat):
        return Verdict(refused="OmniSight's own settings, history and logs are off limits.")

    warnings: list[str] = []
    if re.search(r"[|]", text.replace("||", "")):
        warnings.append("uses a pipe")
    if re.search(r">>?", text):
        warnings.append("writes output to a file or device (redirect)")
    if re.search(r"&&|\|\||;|(?<!&)&(?!&)", text):
        warnings.append("chains several commands")

    for segment in _SEGMENT.split(text):
        tokens = _tokens(segment)
        verb = _verb(tokens)
        if not verb:
            continue
        rest = " ".join(tokens[1:])
        if verb in _ALWAYS_REFUSED:
            return Verdict(refused=f"'{verb}' {_ALWAYS_REFUSED[verb]}.")
        if verb in {"powershell", "pwsh"} and any(t in _POWERSHELL_ENCODED for t in tokens[1:]):
            return Verdict(refused="It hides its real command in an encoded string.")
        if verb in _NESTED_SHELLS:
            inner = _inner_command(segment, tokens)
            if inner:
                nested = check_command(inner)
                if nested.refused:
                    return Verdict(refused=nested.refused + " (inside a nested shell)")
                warnings.extend(nested.warnings)
        if verb == "reg" and tokens[1:2] and tokens[1] in _REG_WRITES:
            return Verdict(refused="It changes the Windows registry.")
        if verb == "netsh" and ("advfirewall" in rest or "firewall" in rest):
            return Verdict(refused="It changes the firewall.")
        if verb == "net" and re.search(r"\b(user|localgroup|group)\b.*\s/add\b", rest):
            return Verdict(refused="It creates a user account or group.")
        if verb == "sc" and re.match(r"(stop|delete|config)\b", rest):
            return Verdict(refused="It changes a Windows service.")
        if verb in _DELETE_VERBS:
            if _is_dangerous_delete(tokens):
                return Verdict(refused="It would delete a whole drive, your home folder or a Windows folder.")
            warnings.append("deletes files")
        elif verb in _INSTALL_VERBS and re.search(r"\b(install|uninstall|add|remove|update|upgrade)\b", rest):
            warnings.append("installs or removes software")
        elif verb in {"git", "gh"} and re.search(r"\b(push|reset --hard|clean|checkout --|rebase|force|branch -d|branch -D)\b", rest, re.IGNORECASE):
            warnings.append("can rewrite or publish git history")
        elif verb in {"taskkill", "stop-process", "kill", "pkill"}:
            warnings.append("ends running programs")
        elif verb in {"setx", "schtasks", "start-process", "start"}:
            warnings.append("starts programs or changes settings that outlast this command")
        elif verb in {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "ssh", "scp", "ftp"}:
            warnings.append("uses the network")
    return Verdict(refused=None, warnings=tuple(dict.fromkeys(warnings)))


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def digest_of(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Approval:
    """The user's yes: the exact text, the shell, and what they typed. Verified again at run time."""

    command: str
    shell: str
    typed: str
    digest: str

    @classmethod
    def create(cls, command: str, shell: str, typed: str) -> Approval:
        return cls(command=command, shell=shell, typed=typed, digest=digest_of(command))

    def problem(self) -> str | None:
        """Why this is not a valid approval (``None`` when it is)."""
        if self.typed != CONFIRM_WORD:
            return f"Type {CONFIRM_WORD} (capital letters) to approve."
        if self.shell not in {"powershell", "cmd"}:
            return "Unknown shell."
        if digest_of(self.command) != self.digest:
            return "The command changed after it was shown."
        return check_command(self.command).refused


class ActionRefused(Exception):
    """The command was not run; the message says why."""


@dataclass
class RunResult:
    exit_code: int | None
    output: str
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    duration_s: float = 0.0
    output_bytes: int = 0


# ---------------------------------------------------------------------------
# Environment, audit log, process
# ---------------------------------------------------------------------------

_SECRET_NAME = re.compile(
    r"(token|secret|passw|credential|api[_-]?key|private[_-]?key|_key$|^key$|auth|cookie|session|^github_|^gemini_|^openai|^anthropic)",
    re.IGNORECASE,
)


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` without anything that looks like a credential."""
    return {name: value for name, value in env.items() if not _SECRET_NAME.search(name)}


def default_audit_path() -> Path:
    appdata = os.environ.get("APPDATA")
    return (Path(appdata) / "OmniSight" if appdata else Path.home() / ".omnisight") / "logs" / "actions.log"


class AuditLog:
    """Append-only JSON lines of every decision about a command (the command text, never its output)."""

    def __init__(self, path: Path | None = None, *, max_bytes: int = AUDIT_MAX_BYTES, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path if path is not None else default_audit_path()
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def record(self, decision: str, *, command: str, shell: str, cwd: str = "", **extra: Any) -> None:
        entry = {
            "time": self._clock().isoformat(timespec="seconds"),
            "decision": decision,
            "shell": shell,
            "cwd": cwd,
            "sha256": digest_of(command),
            "command": command,
            **extra,
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("could not write the action log: %s", exc)

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < self._max_bytes:
                return
        except FileNotFoundError:
            return
        for index in range(AUDIT_BACKUPS, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index - 1}") if index > 1 else self.path
            target = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, target)

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries


class ProcessLike(Protocol):
    stdout: Any

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


_CREATE_NO_WINDOW = 0x08000000
_CREATE_SUSPENDED = 0x00000004


def default_spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> ProcessLike:
    flags = (_CREATE_NO_WINDOW | _CREATE_SUSPENDED) if os.name == "nt" else 0  # suspended: joined to the job before it runs
    return subprocess.Popen(  # noqa: S603 - argv list, shell=False; the text is what the user approved
        argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags
    )


def default_resume(process: ProcessLike) -> None:
    if os.name != "nt":
        return
    import ctypes

    ctypes.WinDLL("ntdll").NtResumeProcess(ctypes.c_void_p(int(getattr(process, "_handle"))))  # noqa: B009 - Popen's Win32 handle


def build_argv(shell: str, command: str) -> list[str]:
    if shell == "cmd":
        return ["cmd.exe", "/d", "/c", command]
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]


def decode_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("oem", errors="replace")
        except LookupError:  # not Windows
            return data.decode("utf-8", errors="replace")


class ActionRunner:
    """Runs an approved command. Everything OS-specific is injectable."""

    def __init__(
        self,
        *,
        spawn: Callable[[list[str], Path, dict[str, str]], ProcessLike] = default_spawn,
        resume: Callable[[ProcessLike], None] = default_resume,
        job_factory: Callable[[], JobLike | None] = default_job,
        audit: AuditLog | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spawn = spawn
        self._resume = resume
        self._job_factory = job_factory
        self.audit = audit if audit is not None else AuditLog()
        self._environ = environ
        self._clock = clock

    def refuse(self, command: str, shell: str, reason: str, cwd: str = "") -> None:
        self.audit.record("refused", command=command, shell=shell, cwd=cwd, reason=reason)

    def cancel_record(self, command: str, shell: str, cwd: str = "") -> None:
        self.audit.record("cancelled", command=command, shell=shell, cwd=cwd)

    def run(
        self,
        approval: Approval,
        cwd: Path,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        cancel: threading.Event | None = None,
    ) -> RunResult:
        """Run ``approval.command``. Raises ``ActionRefused`` (and logs it) unless the approval is complete and valid."""
        problem = approval.problem()
        if problem is None and not Path(cwd).is_dir():
            problem = f"The working folder does not exist: {cwd}"
        if problem is not None:
            self.audit.record("refused", command=approval.command, shell=approval.shell, cwd=str(cwd), reason=problem)
            raise ActionRefused(problem)
        timeout_s = max(1.0, min(float(timeout_s), MAX_TIMEOUT_S))
        env = scrub_env(self._environ if self._environ is not None else os.environ)
        argv = build_argv(approval.shell, approval.command)
        job = self._job_factory()
        started = self._clock()
        try:
            process = self._spawn(argv, Path(cwd), env)
        except OSError as exc:
            self.audit.record("failed", command=approval.command, shell=approval.shell, cwd=str(cwd), error=type(exc).__name__)
            raise ActionRefused(f"Could not start the command: {exc}") from exc
        try:
            if job is not None:
                job.assign(process)
            self._resume(process)
        except OSError as exc:
            process.kill()
            if job is not None:
                job.close()
            self.audit.record("failed", command=approval.command, shell=approval.shell, cwd=str(cwd), error=type(exc).__name__)
            raise ActionRefused(f"Could not start the command safely: {exc}") from exc

        chunks: list[bytes] = []
        state = {"total": 0, "truncated": False}

        def pump() -> None:
            stream = process.stdout
            while stream is not None:
                data = stream.read(4096)
                if not data:
                    break
                state["total"] += len(data)
                room = MAX_OUTPUT_BYTES - sum(len(c) for c in chunks)
                if room > 0:
                    chunks.append(data[:room])
                if len(data) > room:
                    state["truncated"] = True  # keep draining so the command never blocks on a full pipe

        reader = threading.Thread(target=pump, name="omnisight-action-output", daemon=True)
        reader.start()
        deadline = started + timeout_s
        timed_out = cancelled = False
        exit_code: int | None = None
        while True:
            try:
                exit_code = process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if self._clock() >= deadline:
                timed_out = True
                break
        if timed_out or cancelled:
            if job is not None:
                job.close()  # kills the whole tree
            process.kill()
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = None
        reader.join(timeout=5)
        if job is not None and not (timed_out or cancelled):
            job.close()  # normal exit: also ends any background children the command left behind
        result = RunResult(
            exit_code=exit_code,
            output=decode_output(b"".join(chunks)),
            truncated=bool(state["truncated"]),
            timed_out=timed_out,
            cancelled=cancelled,
            duration_s=round(self._clock() - started, 2),
            output_bytes=int(state["total"]),
        )
        self.audit.record(
            "approved",
            command=approval.command,
            shell=approval.shell,
            cwd=str(cwd),
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            duration_s=result.duration_s,
            output_bytes=result.output_bytes,
        )
        return result


@dataclass
class ActionSettings:
    """What the user chose for this session (the master switch is persisted by the controller)."""

    enabled: bool = False
    cwd: Path = field(default_factory=Path.home)


__all__ = [
    "CONFIRM_WORD",
    "SHELL_LANGUAGES",
    "ActionRefused",
    "ActionRunner",
    "ActionSettings",
    "Approval",
    "AuditLog",
    "RunResult",
    "Verdict",
    "build_argv",
    "check_command",
    "digest_of",
    "is_shell_language",
    "scrub_env",
    "shell_for",
]
