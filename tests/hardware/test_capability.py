"""Hardware capability detection and the local-backend recommendation.

Parser and recommendation tests use canned tool output and run everywhere (CI
included). ``test_real_probe_on_this_machine`` is marked ``hardware``: it probes the
actual CPU, RAM and GPU and prints the report (run with ``pytest -m hardware -s``).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from core import capability as cap
from core.capability import Capability, CpuInfo, GpuInfo, parse_cpuinfo, parse_meminfo, parse_nvidia_smi, recommend_backend

RTX_4060 = "NVIDIA GeForce RTX 4060 Laptop GPU, 8188, 1503, 616.92, 8.9"
T4_PAIR = "Tesla T4, 15360, 3, 535.104.05, 7.5\nTesla T4, 15360, 0, 535.104.05, 7.5"
OLD_GPU = "GeForce GTX 750 Ti, 2048, 100, 470.00, 5.0"

CPUINFO = """processor\t: 0
model name\t: Intel(R) Xeon(R) CPU @ 2.20GHz
flags\t\t: fpu vme sse sse2 avx avx2 fma
physical id\t: 0
core id\t\t: 0

processor\t: 1
model name\t: Intel(R) Xeon(R) CPU @ 2.20GHz
flags\t\t: fpu vme sse sse2 avx avx2 fma
physical id\t: 0
core id\t\t: 1
"""

MEMINFO = """MemTotal:       32768000 kB
MemFree:         1024000 kB
MemAvailable:   12288000 kB
"""


def machine(*, free_vram: int | None = None, ram_free: int = 16_000, threads: int = 32, cc: tuple[int, int] = (8, 9)) -> Capability:
    gpus = () if free_vram is None else (GpuInfo("Test GPU", 16_000, 16_000 - free_vram, "600.0", cc),)
    return Capability(cpu=CpuInfo("Test CPU", threads, 24, True, False), ram_total_mb=32_000, ram_free_mb=ram_free, gpus=gpus)


# -- parsers ---------------------------------------------------------------------------


def test_parse_nvidia_smi_single_and_multi_gpu() -> None:
    (gpu,) = parse_nvidia_smi(RTX_4060)
    assert (gpu.name, gpu.total_mb, gpu.used_mb, gpu.free_mb, gpu.driver, gpu.compute_capability) == (
        "NVIDIA GeForce RTX 4060 Laptop GPU", 8188, 1503, 6685, "616.92", (8, 9)
    )
    assert [g.free_mb for g in parse_nvidia_smi(T4_PAIR)] == [15357, 15360]


@pytest.mark.parametrize("text", ["", "garbage", "a, b, c", "GPU, not-a-number, 1, 1.0, 8.9", "\n\n"])
def test_parse_nvidia_smi_ignores_malformed_lines(text: str) -> None:
    assert parse_nvidia_smi(text) == ()


def test_parse_nvidia_smi_tolerates_unknown_compute_capability() -> None:
    (gpu,) = parse_nvidia_smi("Some GPU, 4096, 0, 1.0, [N/A]")
    assert gpu.compute_capability is None


def test_parse_meminfo_prefers_available_memory() -> None:
    assert parse_meminfo(MEMINFO) == (32000, 12000)
    assert parse_meminfo("MemTotal: 2048 kB\nMemFree: 1024 kB") == (2, 1)
    assert parse_meminfo("") == (None, None)


def test_parse_cpuinfo_flags_and_cores() -> None:
    assert parse_cpuinfo(CPUINFO) == ("Intel(R) Xeon(R) CPU @ 2.20GHz", True, False, 2)
    assert parse_cpuinfo("model name : ARM\n") == ("ARM", None, None, None)


# -- recommendation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "device", "model", "dtype", "backend"),
    [
        ({"free_vram": 15_000}, "cuda", "7b", None, "auto"),
        ({"free_vram": 7_500}, "cuda", "7b", None, "auto"),
        ({"free_vram": 6_685}, "cuda", "2b", None, "auto"),  # the RTX 4060 Laptop measured case
        ({"free_vram": 4_000}, "cuda", "2b", None, "auto"),
        ({"free_vram": 3_000, "ram_free": 16_000}, "cpu", "2b", "float32", "auto"),
        ({"free_vram": None, "ram_free": 10_500}, "cpu", "2b", "float32", "auto"),
        ({"free_vram": None, "ram_free": 8_000}, "cpu", "2b", "int8", "auto"),
        ({"free_vram": None, "ram_free": 3_000}, None, None, None, "kaggle"),
        ({"free_vram": None, "ram_free": 16_000, "threads": 2}, None, None, None, "kaggle"),
        ({"free_vram": 12_000, "cc": (5, 0), "ram_free": 3_000}, None, None, None, "kaggle"),
    ],
    ids=["t4", "7b-edge", "rtx4060", "2b-edge", "small-gpu-cpu-fp32", "no-gpu-fp32-edge", "no-gpu-int8", "no-gpu-low-ram", "two-threads", "too-old-gpu"],
)
def test_recommend_backend(kwargs: dict[str, Any], device: str | None, model: str | None, dtype: str | None, backend: str) -> None:
    rec = recommend_backend(machine(**kwargs))
    assert (rec.device, rec.model, rec.cpu_dtype, rec.client_backend) == (device, model, dtype, backend)
    assert rec.reason and rec.summary


def test_describe_is_one_readable_line() -> None:
    line = cap.describe(machine(free_vram=6_685))
    assert "\n" not in line
    assert "Test CPU (24 cores/32 threads, AVX2)" in line
    assert "Best local option: Local GPU (2B NF4)" in line
    assert "no NVIDIA GPU" in cap.describe(machine(free_vram=None))


def test_capability_serializes_for_reports() -> None:
    data = machine(free_vram=6_685).to_dict()
    assert data["gpus"][0]["free_mb"] == 6_685 and data["cpu"]["avx2"] is True


# -- probes with fake tools -----------------------------------------------------------------


def test_probe_gpus_parses_tool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", lambda name: "nvidia-smi")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        assert kwargs["timeout"] == cap.NVIDIA_SMI_TIMEOUT_S
        return SimpleNamespace(returncode=0, stdout=RTX_4060 + "\n")

    (gpu,) = cap.probe_gpus(fake_run)
    assert gpu.name.endswith("RTX 4060 Laptop GPU")
    assert calls[0][1].startswith("--query-gpu=")


@pytest.mark.parametrize(
    "behavior",
    ["missing", "timeout", "error-exit", "oserror"],
)
def test_probe_gpus_is_empty_when_the_tool_fails(monkeypatch: pytest.MonkeyPatch, behavior: str) -> None:
    monkeypatch.setattr(cap.shutil, "which", lambda name: None if behavior == "missing" else "nvidia-smi")

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        if behavior == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if behavior == "oserror":
            raise OSError("driver not loaded")
        return SimpleNamespace(returncode=9, stdout="")

    assert cap.probe_gpus(fake_run) == ()


def test_probe_returns_a_complete_capability_without_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", lambda name: None)
    result = cap.probe()
    assert result.cpu.logical_threads >= 1 and result.cpu.name
    assert result.gpus == () and result.best_gpu is None
    assert result.ram_total_mb is None or result.ram_total_mb > 0


@pytest.mark.hardware
def test_real_probe_on_this_machine(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    result = cap.probe()
    rec = recommend_backend(result)
    with capsys.disabled():
        print("\n" + cap.describe(result, rec))
        print(json.dumps({"capability": result.to_dict(), "recommendation": rec.__dict__}, indent=2, default=str))
    assert result.cpu.logical_threads >= 1
    assert result.ram_total_mb and result.ram_total_mb > 1024
    assert result.ram_free_mb is not None and 0 < result.ram_free_mb <= result.ram_total_mb
    if cap.shutil.which("nvidia-smi"):
        assert result.gpus, "nvidia-smi is installed but no GPU was parsed"
        assert all(g.total_mb > 0 for g in result.gpus)
