"""CPU backend of the local node (run in .venv-gpu; no GPU is used even if one exists).

    .venv-gpu\\Scripts\\python -m pytest -m cpu_inference tests/hardware/test_local_cpu.py -s

* device resolution and the free-RAM preflight (fast, torch only);
* ``model``: Qwen2-VL-2B unquantized on this CPU, reading a rendered traceback, with
  latency budgets derived from the measured run (i9-13980HX, float32, 896x504 pixels:
  first token ~15 s, ~5 tok/s) plus generous margin for slower CPUs and background load.
"""

from __future__ import annotations

import gc
from types import SimpleNamespace
from typing import Any

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("psutil")

import node_config  # noqa: E402
import omnisight_contracts as oc  # noqa: E402

MODEL_2B = "Qwen/Qwen2-VL-2B-Instruct"
CPU_SETTINGS: dict[str, Any] = {
    "model_id": MODEL_2B,
    "device": "cpu",
    "cpu_dtype": "float32",
    "max_pixels": 896 * 504,
    "cpu_max_new_tokens": 128,
    "generation_timeout_s": 240.0,
    "queue_timeout_s": 300.0,
    "asr_preload": False,
}
TTFT_BUDGET_MS = 45_000  # measured ~14.6 s (OCR) on the dev laptop
DECODE_FLOOR_TPS = 2.0  # measured ~5.3 tok/s


@pytest.mark.cpu_inference
@pytest.mark.parametrize(
    ("device", "cuda", "expected"),
    [("cpu", True, "cpu"), ("cpu", False, "cpu"), ("auto", False, "cpu"), ("auto", True, "cuda"), ("cuda", True, "cuda")],
)
def test_device_resolution(monkeypatch: pytest.MonkeyPatch, device: str, cuda: bool, expected: str) -> None:
    import engine

    monkeypatch.setattr(engine.torch.cuda, "is_available", lambda: cuda)
    assert engine.resolve_device(node_config.ServerSettings(device=device)).type == expected


@pytest.mark.cpu_inference
def test_cpu_mode_refuses_to_start_without_enough_free_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    from engine import QwenVisionEngine
    from engine_api import UnsupportedHardwareError

    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=2 * 1024**3))
    engine = QwenVisionEngine(node_config.ServerSettings(**CPU_SETTINGS))
    with pytest.raises(UnsupportedHardwareError, match="needs about 10500 MB of free RAM"):
        engine.load()
    assert engine.state == "failed"
    health = engine.health(queue_depth=0, uptime_s=1.0)
    assert health.status == "degraded" and "free RAM" in (health.detail or "")


@pytest.mark.cpu_inference
def test_cpu_settings_are_parsed_from_the_environment() -> None:
    settings = node_config.ServerSettings.from_environment(
        {"OMNISIGHT_DEVICE": "CPU", "OMNISIGHT_CPU_DTYPE": "Float32", "OMNISIGHT_CPU_THREADS": "8", "OMNISIGHT_CPU_MAX_NEW_TOKENS": "200"},
        use_kaggle_secrets=False,
    )
    assert (settings.device, settings.cpu_dtype, settings.cpu_threads, settings.cpu_max_new_tokens) == ("cpu", "float32", 8, 200)
    assert settings.model_label.endswith("-cpu-float32")


@pytest.fixture(scope="module")
def cpu_engine() -> Any:
    from engine import QwenVisionEngine

    engine = QwenVisionEngine(node_config.ServerSettings(**CPU_SETTINGS))
    engine.load()
    yield engine
    del engine
    gc.collect()


@pytest.mark.cpu_inference
@pytest.mark.model
@pytest.mark.slow
@pytest.mark.timeout(1200)
def test_real_2b_on_this_cpu_reads_the_traceback(cpu_engine: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from benchmark import prepare_samples

    sample = prepare_samples(["python_traceback"], [(1280, 720)])[0]
    answer = cpu_engine.analyze(
        oc.AnalyzeRequest(mode="ocr", image=sample.payload, max_new_tokens=512, temperature=0), queue_ms=0.0
    )
    health = cpu_engine.health(queue_depth=0, uptime_s=1.0)
    with capsys.disabled():
        t = answer.timings
        print(f"\n[cpu ocr] ttft {t.ttft_ms / 1000:.1f} s, total {t.total_ms / 1000:.1f} s, {t.tokens_per_sec:.2f} tok/s, {t.tokens_generated} tokens")
        print("   warnings:", health.warnings)
    assert cpu_engine.device.type == "cpu"
    assert "discount_rate" in answer.markdown
    assert answer.timings.tokens_generated <= CPU_SETTINGS["cpu_max_new_tokens"]  # the CPU cap applied (asked for 512)
    assert answer.timings.ttft_ms <= TTFT_BUDGET_MS
    assert answer.timings.tokens_per_sec >= DECODE_FLOOR_TPS or answer.timings.tokens_generated <= 1
    assert health.gpu_available is False and health.quantization == "none"
    assert health.vram_allocated_mb == 0.0 and health.baseline_vram_mb is None
    assert any("running on the CPU" in w for w in health.warnings)
    assert health.gpu_name and "(CPU)" not in health.gpu_name  # the CPU's own name


@pytest.mark.cpu_inference
@pytest.mark.model
@pytest.mark.slow
@pytest.mark.timeout(1200)
def test_cpu_node_serves_the_http_contract(cpu_engine: Any) -> None:
    from fastapi.testclient import TestClient

    import server
    from tests.support import small_jpeg_payload

    app = server.create_app(cpu_engine, node_config.ServerSettings(**CPU_SETTINGS))
    with TestClient(app, raise_server_exceptions=False) as client:
        health = client.get("/v1/health").json()
        assert health["model_loaded"] and not health["gpu_available"]
        response = client.post(
            "/v1/analyze", json={"mode": "summarize", "image": small_jpeg_payload(), "max_new_tokens": 32, "temperature": 0}
        )
        assert response.status_code == 200, response.text
        assert oc.AnalyzeResponse.model_validate(response.json()).markdown


@pytest.mark.cpu_inference
@pytest.mark.model
@pytest.mark.slow
@pytest.mark.timeout(1200)
def test_cpu_chat_follow_up_uses_earlier_turns_without_an_image(cpu_engine: Any, capsys: pytest.CaptureFixture[str]) -> None:
    history = [
        oc.ChatTurn(role="user", text="Why does this program crash?"),
        oc.ChatTurn(role="assistant", text="It raises a KeyError because the key 'discount_rate' is missing from the config dict."),
    ]
    answer = cpu_engine.analyze(
        oc.AnalyzeRequest(
            mode="chat", prompt="Which key was missing, in the conversation above? Answer with the key name.", history=history,
            max_new_tokens=32, temperature=0,
        ),
        queue_ms=0.0,
    )
    with capsys.disabled():
        t = answer.timings
        print(f"\n[cpu chat+history] ttft {t.ttft_ms / 1000:.1f} s, {t.tokens_per_sec:.2f} tok/s: {answer.markdown[:80]!r}")
    assert "discount_rate" in answer.markdown
    assert answer.timings.ttft_ms <= TTFT_BUDGET_MS
