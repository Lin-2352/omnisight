"""What this PC can run locally: CPU, RAM and NVIDIA GPU capability plus a backend recommendation.

Dependency-free and read-only (no admin rights, nothing written): CPU flags come from
``IsProcessorFeaturePresent`` on Windows or ``/proc/cpuinfo`` on Linux, memory from
``GlobalMemoryStatusEx`` or ``/proc/meminfo``, GPUs from ``nvidia-smi`` (2 s timeout).
Used by the client (startup log, Settings dialog), ``scripts/capability_report.py`` and
``scripts/run-local-gpu.ps1 -Device auto``.

Thresholds come from measurements on the project hardware:

* Qwen2-VL-7B NF4 on a GPU peaked at 7475 MB (Kaggle T4), so it needs ~7.5 GB free VRAM;
* Qwen2-VL-2B NF4 peaked at 3167 MB (RTX 4060 Laptop), so 4 GB free VRAM is enough;
* bitsandbytes 4-bit kernels need compute capability 6.0 or newer (the engine refuses older);
* on the CPU (i9-13980HX, AVX2), 2B in float32 peaked at 10.5 GB RAM with correct answers
  (~14 s to the first token, ~5 tok/s); int8 needs 7.1 GB but answers noticeably worse;
  bfloat16 is only fast on CPUs with native bf16 math (AVX-512 BF16 / AMX).
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

GPU_7B_FREE_MB: Final[int] = 7_500
GPU_2B_FREE_MB: Final[int] = 4_000
MIN_COMPUTE_CAPABILITY: Final[tuple[int, int]] = (6, 0)
CPU_FP32_FREE_MB: Final[int] = 10_500
CPU_INT8_FREE_MB: Final[int] = 7_500
CPU_MIN_THREADS: Final[int] = 4
NVIDIA_SMI_TIMEOUT_S: Final[float] = 2.0
NVIDIA_SMI_QUERY: Final[tuple[str, ...]] = (
    "--query-gpu=name,memory.total,memory.used,driver_version,compute_cap",
    "--format=csv,noheader,nounits",
)

Device = Literal["cuda", "cpu"]


@dataclass(frozen=True)
class GpuInfo:
    name: str
    total_mb: int
    used_mb: int
    driver: str
    compute_capability: tuple[int, int] | None

    @property
    def free_mb(self) -> int:
        return max(0, self.total_mb - self.used_mb)


@dataclass(frozen=True)
class CpuInfo:
    name: str
    logical_threads: int
    physical_cores: int | None = None
    avx2: bool | None = None
    avx512: bool | None = None


@dataclass(frozen=True)
class Capability:
    cpu: CpuInfo
    ram_total_mb: int | None
    ram_free_mb: int | None
    gpus: tuple[GpuInfo, ...] = ()
    os: str = field(default_factory=platform.platform)

    @property
    def best_gpu(self) -> GpuInfo | None:
        usable = [g for g in self.gpus if g.compute_capability is None or g.compute_capability >= MIN_COMPUTE_CAPABILITY]
        return max(usable, key=lambda g: g.free_mb, default=None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gpus"] = [{**asdict(g), "free_mb": g.free_mb} for g in self.gpus]
        return data


@dataclass(frozen=True)
class Recommendation:
    """The best way to run OmniSight on this PC."""

    device: Device | None  # where a local node should run; None = no usable local option
    model: Literal["7b", "2b"] | None
    cpu_dtype: Literal["float32", "int8"] | None
    client_backend: Literal["auto", "kaggle"]
    reason: str

    @property
    def summary(self) -> str:
        if self.device == "cuda":
            return f"Local GPU ({self.model.upper() if self.model else '?'} NF4)"
        if self.device == "cpu":
            return f"Local CPU (2B {self.cpu_dtype}, slow)"
        return "Kaggle cloud GPU"


# ---------------------------------------------------------------------------
# Parsers (pure; unit-tested with canned text)
# ---------------------------------------------------------------------------


def parse_nvidia_smi(text: str) -> tuple[GpuInfo, ...]:
    """Parse ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits`` output."""
    gpus: list[GpuInfo] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5 or not parts[0]:
            continue
        name, total, used, driver, cc = parts
        try:
            total_mb, used_mb = int(float(total)), int(float(used))
        except ValueError:
            continue
        capability: tuple[int, int] | None = None
        major, _, minor = cc.partition(".")
        if major.isdigit() and minor.isdigit():
            capability = (int(major), int(minor))
        gpus.append(GpuInfo(name=name, total_mb=total_mb, used_mb=used_mb, driver=driver, compute_capability=capability))
    return tuple(gpus)


def parse_meminfo(text: str) -> tuple[int | None, int | None]:
    """(total_mb, available_mb) from Linux ``/proc/meminfo``."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        number = rest.strip().split(" ")[0]
        if number.isdigit():
            values[key.strip()] = int(number) // 1024  # kB -> MB (binary, as the kernel reports)
    available = values.get("MemAvailable", values.get("MemFree"))
    return values.get("MemTotal"), available


def parse_cpuinfo(text: str) -> tuple[str | None, bool | None, bool | None, int | None]:
    """(model name, avx2, avx512f, physical cores) from Linux ``/proc/cpuinfo``."""
    name: str | None = None
    flags: set[str] = set()
    cores: set[tuple[str, str]] = set()
    physical_id = "0"
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "model name" and name is None:
            name = value
        elif key == "flags" and not flags:
            flags = set(value.split())
        elif key == "physical id":
            physical_id = value
        elif key == "core id":
            cores.add((physical_id, value))
    if not flags:
        return name, None, None, len(cores) or None
    return name, "avx2" in flags, "avx512f" in flags, len(cores) or None


# ---------------------------------------------------------------------------
# Probes (read-only)
# ---------------------------------------------------------------------------


def _windows_cpu() -> CpuInfo:
    name = platform.processor() or "unknown CPU"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or name
    except OSError:
        pass
    avx2 = avx512 = None
    try:
        present = ctypes.windll.kernel32.IsProcessorFeaturePresent  # type: ignore[attr-defined]
        avx2 = bool(present(40))  # PF_AVX2_INSTRUCTIONS_AVAILABLE
        avx512 = bool(present(41))  # PF_AVX512F_INSTRUCTIONS_AVAILABLE
    except (AttributeError, OSError):
        pass
    return CpuInfo(name=name, logical_threads=os.cpu_count() or 1, physical_cores=_physical_cores(), avx2=avx2, avx512=avx512)


def _physical_cores() -> int | None:
    try:
        import psutil  # optional

        return psutil.cpu_count(logical=False)
    except ImportError:
        return None


def _windows_memory() -> tuple[int | None, int | None]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None, None
    except (AttributeError, OSError):
        return None, None
    return status.ullTotalPhys // (1024 * 1024), status.ullAvailPhys // (1024 * 1024)


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def probe_gpus(run: Callable[..., Any] = subprocess.run) -> tuple[GpuInfo, ...]:
    """NVIDIA GPUs via nvidia-smi; empty when the tool is missing, slow or failing."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return ()
    try:
        result = run([exe, *NVIDIA_SMI_QUERY], capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return ()
    if getattr(result, "returncode", 1) != 0:
        return ()
    return parse_nvidia_smi(getattr(result, "stdout", "") or "")


def probe(run: Callable[..., Any] = subprocess.run) -> Capability:
    """Measure this machine (read-only, typically well under a second)."""
    if sys.platform == "win32":
        cpu = _windows_cpu()
        total, free = _windows_memory()
    else:
        name, avx2, avx512, cores = parse_cpuinfo(_read("/proc/cpuinfo"))
        cpu = CpuInfo(
            name=name or platform.processor() or "unknown CPU",
            logical_threads=os.cpu_count() or 1,
            physical_cores=cores or _physical_cores(),
            avx2=avx2,
            avx512=avx512,
        )
        total, free = parse_meminfo(_read("/proc/meminfo"))
    return Capability(cpu=cpu, ram_total_mb=total, ram_free_mb=free, gpus=probe_gpus(run))


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def recommend_backend(capability: Capability) -> Recommendation:
    """Pick the best local option (GPU 7B > GPU 2B > CPU 2B) or fall back to Kaggle."""
    gpu = capability.best_gpu
    if gpu is not None and gpu.free_mb >= GPU_7B_FREE_MB:
        return Recommendation("cuda", "7b", None, "auto", f"{gpu.name} has {gpu.free_mb} MB free VRAM (7B needs {GPU_7B_FREE_MB})")
    if gpu is not None and gpu.free_mb >= GPU_2B_FREE_MB:
        return Recommendation(
            "cuda", "2b", None, "auto",
            f"{gpu.name} has {gpu.free_mb} MB free VRAM: enough for 2B ({GPU_2B_FREE_MB}), not 7B ({GPU_7B_FREE_MB})",
        )
    free = capability.ram_free_mb or 0
    threads = capability.cpu.logical_threads
    gpu_note = f"{gpu.name} has only {gpu.free_mb} MB free VRAM; " if gpu is not None else "no usable NVIDIA GPU; "
    if threads >= CPU_MIN_THREADS and free >= CPU_FP32_FREE_MB:
        return Recommendation("cpu", "2b", "float32", "auto", gpu_note + f"{free} MB free RAM runs 2B on the CPU in float32 (slow)")
    if threads >= CPU_MIN_THREADS and free >= CPU_INT8_FREE_MB:
        return Recommendation(
            "cpu", "2b", "int8", "auto",
            gpu_note + f"{free} MB free RAM runs 2B on the CPU in int8 (slow, lower answer quality)",
        )
    return Recommendation(None, None, None, "kaggle", gpu_note + f"{free} MB free RAM is not enough for a local model; use Kaggle")


def describe(capability: Capability, recommendation: Recommendation | None = None) -> str:
    """One line for logs and the Settings dialog."""
    rec = recommendation or recommend_backend(capability)
    cpu = capability.cpu
    flags = ", ".join(flag for flag, on in (("AVX2", cpu.avx2), ("AVX-512", cpu.avx512)) if on) or "no AVX2"
    cores = f"{cpu.physical_cores} cores/" if cpu.physical_cores else ""
    ram = f"{(capability.ram_total_mb or 0) / 1024:.0f} GB RAM ({(capability.ram_free_mb or 0) / 1024:.1f} GB free)"
    gpu = capability.best_gpu
    gpu_text = f"{gpu.name} {gpu.total_mb / 1024:.0f} GB ({gpu.free_mb / 1024:.1f} GB free)" if gpu else "no NVIDIA GPU"
    return f"{cpu.name} ({cores}{cpu.logical_threads} threads, {flags}) - {ram} - {gpu_text}. Best local option: {rec.summary}."
