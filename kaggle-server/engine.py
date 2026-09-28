"""Qwen2-VL-7B (NF4) + Whisper inference engine. The only module that imports torch.

Memory accounting uses ``torch.cuda.memory_allocated`` in decimal units
(1 GB = 10**9 bytes). nvidia-smi reports more because it also counts the CUDA
context and the caching allocator's reserve.

TTFT definition: the clock starts once the GPU lock is held, before image
decoding and preprocessing, and stops when the first new token has been
sampled (after a CUDA sync). It therefore includes preprocessing, the vision
encoder, and prefill. Time spent waiting for the GPU is reported as queue_ms.

tokens_per_sec is decode throughput: (tokens_generated - 1) / (end - first token).
"""

from __future__ import annotations

import gc
import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import torch
from PIL import Image
from transformers import (
    AutoProcessor,
    BitsAndBytesConfig,
    Qwen2VLForConditionalGeneration,
    StoppingCriteria,
    StoppingCriteriaList,
    WhisperForConditionalGeneration,
    WhisperProcessor,
)

from omnisight_contracts import (
    MAX_MARKDOWN_CHARS,
    MAX_NEW_TOKENS,
    MAX_PROMPT_CHARS,
    AnalyzeRequest,
    AnalyzeResponse,
    AudioPayload,
    HealthResponse,
    InferenceTimings,
    derive_summary,
    extract_code_blocks,
)

from engine_api import (
    EngineState,
    EngineUnavailableError,
    GpuOutOfMemoryError,
    InvalidInputError,
    ModelNotReadyError,
    UnsupportedHardwareError,
    acquire_gpu,
)
from media import ASR_SAMPLE_RATE, load_image, prepare_for_asr
from node_config import GB, MB, ServerSettings
from prompts import WARMUP_MESSAGES, build_messages

logger = logging.getLogger("omnisight.engine")

MIN_COMPUTE_CAPABILITY: Final[tuple[int, int]] = (6, 0)
OOM_DEGRADED_WINDOW_S: Final[float] = 60.0
ASR_MAX_NEW_TOKENS: Final[int] = 220


def describe_gpu(device_index: int = 0) -> str:
    """Label such as ``"Tesla T4 16GB"`` (decimal GB, rounded), or ``"cpu"``."""
    if not torch.cuda.is_available():
        return "cpu"
    props = torch.cuda.get_device_properties(device_index)
    return f"{props.name} {round(props.total_memory / GB)}GB"


class FirstTokenTimer(StoppingCriteria):
    """Records when the first new token has been sampled; never stops generation."""

    def __init__(self) -> None:
        self.first_token_at: float | None = None

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs: Any) -> torch.BoolTensor:
        if self.first_token_at is None:
            torch.cuda.synchronize()
            self.first_token_at = time.perf_counter()
        return torch.zeros(input_ids.shape[0], dtype=torch.bool, device=input_ids.device)


@dataclass(frozen=True)
class GenerationOutcome:
    text: str
    tokens_generated: int
    first_token_at: float | None
    finished_at: float
    finish_reason: str
    confidence: float | None
    input_tokens: int


class QwenVisionEngine:
    """Serves Qwen2-VL-7B-Instruct in 4-bit NF4 on a single CUDA device."""

    def __init__(self, settings: ServerSettings, device_index: int = 0) -> None:
        self.settings = settings
        self.gpu_lock = threading.Lock()
        self._device_index = device_index
        self._device = torch.device(f"cuda:{device_index}")
        self._state: EngineState = "idle"
        self._state_lock = threading.Lock()
        self._asr_lock = threading.Lock()
        self._model: Qwen2VLForConditionalGeneration | None = None
        self._processor: Any = None
        self._asr_model: WhisperForConditionalGeneration | None = None
        self._asr_processor: WhisperProcessor | None = None
        self._asr_bytes: int = 0
        self._load_error: str | None = None
        self._baseline_bytes: int | None = None
        self._total_bytes: int = 0
        self._gpu_name: str | None = None
        self._compute_capability: tuple[int, int] | None = None
        self._oom_events = 0
        self._last_oom_at: float | None = None
        self._eos_ids: frozenset[int] = frozenset()
        self._pad_id: int | None = None

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> EngineState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: EngineState) -> None:
        with self._state_lock:
            self._state = state

    @property
    def baseline_bytes(self) -> int | None:
        return self._baseline_bytes

    # ------------------------------------------------------------------- load

    def load(self) -> None:
        """Validate the GPU, apply the VRAM ceiling, load weights, and warm up."""
        self._set_state("loading")
        started = time.perf_counter()
        try:
            self._check_hardware()
            self._apply_memory_ceiling()
            self._load_model()
            torch.cuda.synchronize(self._device)
            self._baseline_bytes = torch.cuda.memory_allocated(self._device_index)
            budget = self.settings.baseline_budget_bytes
            verdict = "within" if self._baseline_bytes <= budget else "OVER"
            logger.info(
                "model resident: %.0f MB allocated (%s the %.0f MB baseline budget)",
                self._baseline_bytes / MB,
                verdict,
                budget / MB,
            )
            if self.settings.asr_preload:
                self._ensure_asr()
            self._warmup()
            torch.cuda.reset_peak_memory_stats(self._device_index)
            self._set_state("ready")
            logger.info("engine ready in %.1f s", time.perf_counter() - started)
        except Exception as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
            self._set_state("failed")
            logger.exception("engine failed to load")
            raise

    def _check_hardware(self) -> None:
        if not torch.cuda.is_available():
            raise UnsupportedHardwareError(
                "CUDA is not available. In Kaggle, set Accelerator to 'GPU T4 x2' or 'GPU P100'."
            )
        props = torch.cuda.get_device_properties(self._device_index)
        self._gpu_name = props.name
        self._total_bytes = props.total_memory
        self._compute_capability = (props.major, props.minor)
        if self._compute_capability < MIN_COMPUTE_CAPABILITY:
            raise UnsupportedHardwareError(
                f"{props.name} has compute capability sm_{props.major}{props.minor}; "
                f"sm_{MIN_COMPUTE_CAPABILITY[0]}{MIN_COMPUTE_CAPABILITY[1]} or newer is required"
            )
        logger.info(
            "GPU %d: %s, sm_%d%d, %.1f GB, %d device(s) visible",
            self._device_index,
            props.name,
            props.major,
            props.minor,
            props.total_memory / GB,
            torch.cuda.device_count(),
        )
        self._bitsandbytes_smoke_test()

    def _bitsandbytes_smoke_test(self) -> None:
        """Run one NF4 matmul so unsupported GPUs fail before downloading 7B weights."""
        try:
            import bitsandbytes as bnb

            layer = bnb.nn.Linear4bit(
                64, 64, bias=False, compute_dtype=torch.float16, quant_type="nf4"
            ).to(self._device)
            probe = torch.randn(2, 64, dtype=torch.float16, device=self._device)
            with torch.inference_mode():
                result = layer(probe)
            torch.cuda.synchronize(self._device)
            finite = bool(torch.isfinite(result).all().item())
            del layer, probe, result
            torch.cuda.empty_cache()
        except Exception as exc:
            raise UnsupportedHardwareError(
                f"bitsandbytes 4-bit kernels failed on {self._gpu_name} "
                f"(sm_{self._compute_capability[0]}{self._compute_capability[1]}): {exc}"
                if self._compute_capability
                else f"bitsandbytes 4-bit kernels failed: {exc}"
            ) from exc
        if not finite:
            raise UnsupportedHardwareError("bitsandbytes NF4 smoke test produced non-finite values")

    def _apply_memory_ceiling(self) -> None:
        fraction = min(1.0, self.settings.vram_ceiling_bytes / self._total_bytes)
        torch.cuda.set_per_process_memory_fraction(fraction, self._device_index)
        logger.info(
            "VRAM ceiling %.1f GB = %.3f of %.1f GB; allocations beyond it raise OOM (HTTP 507)",
            self.settings.vram_ceiling_gb,
            fraction,
            self._total_bytes / GB,
        )

    def _load_model(self) -> None:
        token = self.settings.hf_token.get_secret_value() if self.settings.hf_token else None
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.float16,
            # None keeps transformers' default exclusions (lm_head stays fp16); an empty
            # list quantizes lm_head too, trading output quality for ~0.8 GB.
            llm_int8_skip_modules=[] if self.settings.quantize_lm_head else None,
        )
        logger.info("loading processor for %s", self.settings.model_id)
        self._processor = AutoProcessor.from_pretrained(
            self.settings.model_id,
            min_pixels=self.settings.min_pixels,
            max_pixels=self.settings.max_pixels,
            token=token,
        )
        logger.info("loading %s in 4-bit NF4 (double quant, fp16 compute)", self.settings.model_id)
        model = Qwen2VLForConditionalGeneration.from_pretrained(
            self.settings.model_id,
            quantization_config=quantization,
            device_map={"": self._device_index},
            torch_dtype=torch.float16,
            attn_implementation="sdpa",
            low_cpu_mem_usage=True,
            token=token,
        )
        model.eval()
        generation_config = model.generation_config
        eos = generation_config.eos_token_id
        eos_ids = [eos] if isinstance(eos, int) else list(eos or [])
        tokenizer_eos = self._processor.tokenizer.eos_token_id
        if tokenizer_eos is not None:
            eos_ids.append(tokenizer_eos)
        self._eos_ids = frozenset(eos_ids)
        self._pad_id = (
            generation_config.pad_token_id
            if generation_config.pad_token_id is not None
            else self._processor.tokenizer.pad_token_id
        )
        self._model = model

    def _ensure_asr(self) -> None:
        with self._asr_lock:
            if self._asr_model is not None:
                return
            token = self.settings.hf_token.get_secret_value() if self.settings.hf_token else None
            before = torch.cuda.memory_allocated(self._device_index)
            logger.info("loading ASR model %s (fp16)", self.settings.asr_model_id)
            processor = WhisperProcessor.from_pretrained(self.settings.asr_model_id, token=token)
            model = WhisperForConditionalGeneration.from_pretrained(
                self.settings.asr_model_id, torch_dtype=torch.float16, token=token
            ).to(self._device)
            model.eval()
            self._asr_processor = processor
            self._asr_model = model
            self._asr_bytes = torch.cuda.memory_allocated(self._device_index) - before
            logger.info("ASR resident: %.0f MB (not counted in the model baseline)", self._asr_bytes / MB)

    def _warmup(self) -> None:
        image = Image.new("RGB", (256, 256), color=(255, 255, 255))
        with acquire_gpu(self.gpu_lock, self.settings.queue_timeout_s):
            text = self._processor.apply_chat_template(
                WARMUP_MESSAGES, tokenize=False, add_generation_prompt=True
            )
            inputs = self._processor(text=[text], images=[image], return_tensors="pt").to(self._device)
            with torch.inference_mode():
                self._model.generate(**inputs, max_new_tokens=4, do_sample=False, pad_token_id=self._pad_id)
            del inputs
        torch.cuda.empty_cache()

    # ---------------------------------------------------------------- analyze

    def analyze(self, request: AnalyzeRequest, *, queue_ms: float) -> AnalyzeResponse:
        state = self.state
        if state in ("idle", "loading"):
            raise ModelNotReadyError("the model is still loading; retry shortly")
        if state == "failed":
            raise EngineUnavailableError(
                "the model failed to load; restart the node", details=[self._load_error or "unknown"]
            )

        with acquire_gpu(self.gpu_lock, self.settings.queue_timeout_s) as lock_wait_ms:
            started = time.perf_counter()
            image = load_image(request.image)
            transcript: str | None = None
            outcome: GenerationOutcome | None = None
            input_tokens = 0
            oom = False
            try:
                if request.audio is not None:
                    transcript = self._transcribe(request.audio)
                outcome = self._generate(request, image, transcript)
                input_tokens = outcome.input_tokens
            except torch.cuda.OutOfMemoryError:
                oom = True
            if oom:
                # Outside the except block: the traceback (and the tensors its frames
                # reference) has been released, so empty_cache can return the memory.
                raise self._recover_from_oom(request, image.size, input_tokens)

        assert outcome is not None
        return self._build_response(request, outcome, transcript, started, queue_ms + lock_wait_ms)

    def _transcribe(self, audio: AudioPayload) -> str:
        samples = prepare_for_asr(audio)
        self._ensure_asr()
        assert self._asr_processor is not None and self._asr_model is not None
        features = self._asr_processor(
            samples, sampling_rate=ASR_SAMPLE_RATE, return_tensors="pt"
        ).input_features.to(self._device, dtype=torch.float16)
        with torch.inference_mode():
            ids = self._asr_model.generate(features, task="transcribe", max_new_tokens=ASR_MAX_NEW_TOKENS)
        text = self._asr_processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
        del features, ids
        if not text:
            raise InvalidInputError("no speech was detected in the audio clip")
        return text[:MAX_PROMPT_CHARS]

    def _generate(self, request: AnalyzeRequest, image: Image.Image, transcript: str | None) -> GenerationOutcome:
        assert self._model is not None
        messages = build_messages(request.mode, request.prompt, transcript)
        text = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self._processor(text=[text], images=[image], return_tensors="pt").to(self._device)
        prompt_length = int(inputs["input_ids"].shape[1])
        max_new_tokens = min(request.max_new_tokens, MAX_NEW_TOKENS)
        timer = FirstTokenTimer()
        do_sample = request.temperature > 0
        sampling: dict[str, Any] = (
            {"do_sample": True, "temperature": request.temperature, "top_p": 1.0, "top_k": 0}
            if do_sample
            else {"do_sample": False, "temperature": None, "top_p": None, "top_k": None}
        )
        with torch.inference_mode():
            output = self._model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                repetition_penalty=self.settings.repetition_penalty,
                max_time=self.settings.generation_timeout_s,
                remove_invalid_values=True,
                return_dict_in_generate=True,
                output_logits=True,
                stopping_criteria=StoppingCriteriaList([timer]),
                pad_token_id=self._pad_id,
                eos_token_id=sorted(self._eos_ids),
                **sampling,
            )
        torch.cuda.synchronize(self._device)
        finished_at = time.perf_counter()

        new_tokens = output.sequences[0, prompt_length:]
        token_list = new_tokens.tolist()
        generated = len(token_list)
        if token_list and token_list[-1] in self._eos_ids:
            finish_reason = "stop"
        elif generated >= max_new_tokens:
            finish_reason = "length"
        else:
            finish_reason = "timeout"
        confidence = self._confidence(output.logits, new_tokens)
        decoded = self._processor.batch_decode(
            new_tokens.unsqueeze(0), skip_special_tokens=True, clean_up_tokenization_spaces=False
        )[0].strip()
        del output, inputs, new_tokens
        return GenerationOutcome(
            text=decoded,
            tokens_generated=generated,
            first_token_at=timer.first_token_at,
            finished_at=finished_at,
            finish_reason=finish_reason,
            confidence=confidence,
            input_tokens=prompt_length,
        )

    @staticmethod
    def _confidence(logits: tuple[torch.Tensor, ...] | None, tokens: torch.Tensor) -> float | None:
        """exp(mean log p) of the chosen tokens under the raw (unwarped) logits."""
        if not logits or tokens.numel() == 0:
            return None
        total = 0.0
        count = min(len(logits), int(tokens.shape[0]))
        for step in range(count):
            row = logits[step][0].float()
            total += float(torch.log_softmax(row, dim=-1)[tokens[step]].item())
        if count == 0 or not math.isfinite(total):
            return None
        return float(min(1.0, max(0.0, math.exp(total / count))))

    def _recover_from_oom(
        self, request: AnalyzeRequest, image_size: tuple[int, int], input_tokens: int
    ) -> GpuOutOfMemoryError:
        peak = torch.cuda.max_memory_allocated(self._device_index)
        gc.collect()
        torch.cuda.empty_cache()
        self._oom_events += 1
        self._last_oom_at = time.monotonic()
        allocated = torch.cuda.memory_allocated(self._device_index)
        reserved = torch.cuda.memory_reserved(self._device_index)
        logger.error(
            "CUDA OOM on request %s (peak %.0f MB, ceiling %.0f MB); cache cleared",
            request.request_id,
            peak / MB,
            self.settings.vram_ceiling_bytes / MB,
        )
        return GpuOutOfMemoryError(
            "GPU memory ceiling exceeded; the cache was cleared, retry with a smaller image or fewer tokens",
            details=[
                f"allocated_mb={allocated / MB:.0f}",
                f"reserved_mb={reserved / MB:.0f}",
                f"peak_mb={peak / MB:.0f}",
                f"ceiling_mb={self.settings.vram_ceiling_bytes / MB:.0f}",
                f"input_tokens={input_tokens}",
                f"image={image_size[0]}x{image_size[1]}",
                f"max_new_tokens={request.max_new_tokens}",
            ],
        )

    def _build_response(
        self,
        request: AnalyzeRequest,
        outcome: GenerationOutcome,
        transcript: str | None,
        started: float,
        queue_ms: float,
    ) -> AnalyzeResponse:
        markdown = outcome.text[:MAX_MARKDOWN_CHARS]
        blocks = extract_code_blocks(markdown)
        languages = Counter(block.language for block in blocks if block.language != "text")
        summary = derive_summary(markdown) if markdown else ""
        if not summary:
            summary = (
                f"The answer consists of {len(blocks)} code block(s)."
                if blocks
                else "The model returned an empty answer."
            )
        total_ms = (outcome.finished_at - started) * 1000.0
        first = outcome.first_token_at or outcome.finished_at
        ttft_ms = min(total_ms, max(0.0, (first - started) * 1000.0))
        decode_s = outcome.finished_at - first
        tokens_per_sec = (outcome.tokens_generated - 1) / decode_s if outcome.tokens_generated > 1 and decode_s > 0 else 0.0
        return AnalyzeResponse(
            request_id=request.request_id,
            model_id=self.settings.model_id,
            source="kaggle",
            summary=summary,
            markdown=markdown,
            code_blocks=blocks,
            detected_language=languages.most_common(1)[0][0] if languages else None,
            transcript=transcript,
            confidence=outcome.confidence,
            finish_reason=outcome.finish_reason,  # type: ignore[arg-type]
            timings=InferenceTimings(
                queue_ms=round(queue_ms, 2),
                ttft_ms=round(ttft_ms, 2),
                total_ms=round(total_ms, 2),
                tokens_generated=outcome.tokens_generated,
                tokens_per_sec=round(tokens_per_sec, 2),
            ),
        )

    # ----------------------------------------------------------------- health

    def health(self, *, queue_depth: int, uptime_s: float) -> HealthResponse:
        state = self.state
        gpu_available = torch.cuda.is_available()
        allocated = reserved = peak = total = 0.0
        gpu_count = 0
        if gpu_available:
            gpu_count = torch.cuda.device_count()
            allocated = torch.cuda.memory_allocated(self._device_index) / MB
            reserved = torch.cuda.memory_reserved(self._device_index) / MB
            peak = torch.cuda.max_memory_allocated(self._device_index) / MB
            total = (self._total_bytes or torch.cuda.get_device_properties(self._device_index).total_memory) / MB
            if self._gpu_name is None:
                self._gpu_name = torch.cuda.get_device_name(self._device_index)

        detail: str | None = None
        if state in ("idle", "loading"):
            status = "loading"
        elif state == "failed":
            status = "degraded"
            detail = f"model failed to load: {self._load_error}"
        else:
            status = "ok"
            problems: list[str] = []
            if self._baseline_bytes is not None and self._baseline_bytes > self.settings.baseline_budget_bytes:
                problems.append(
                    f"baseline {self._baseline_bytes / MB:.0f} MB exceeds the "
                    f"{self.settings.baseline_budget_bytes / MB:.0f} MB budget"
                )
            if self._last_oom_at is not None and time.monotonic() - self._last_oom_at < OOM_DEGRADED_WINDOW_S:
                problems.append("CUDA out-of-memory within the last 60 s")
            if problems:
                status = "degraded"
                detail = "; ".join(problems)

        cc = self._compute_capability
        return HealthResponse(
            status=status,  # type: ignore[arg-type]
            model_id=self.settings.model_id,
            model_loaded=state == "ready",
            quantization="nf4",
            gpu_available=gpu_available,
            gpu_name=self._gpu_name,
            gpu_count=gpu_count,
            vram_allocated_mb=round(allocated, 1),
            vram_reserved_mb=round(reserved, 1),
            vram_total_mb=round(total, 1),
            vram_peak_mb=round(peak, 1),
            vram_ceiling_mb=round(self.settings.vram_ceiling_bytes / MB, 1),
            baseline_vram_mb=round(self._baseline_bytes / MB, 1) if self._baseline_bytes is not None else None,
            compute_capability=f"{cc[0]}.{cc[1]}" if cc else None,
            asr_model_id=self.settings.asr_model_id,
            asr_loaded=self._asr_model is not None,
            queue_depth=queue_depth,
            oom_events=self._oom_events,
            detail=detail,
            uptime_s=round(uptime_s, 1),
        )

    # -------------------------------------------------------------- benchmark

    def reset_peak_memory(self) -> None:
        """Reset the allocator's peak counter (used by the benchmark between runs)."""
        torch.cuda.reset_peak_memory_stats(self._device_index)

    def peak_memory_bytes(self) -> int:
        return int(torch.cuda.max_memory_allocated(self._device_index))

    def runtime_versions(self) -> dict[str, str]:
        import bitsandbytes
        import transformers

        return {
            "torch": torch.__version__,
            "cuda": str(torch.version.cuda),
            "transformers": transformers.__version__,
            "bitsandbytes": bitsandbytes.__version__,
            "numpy": np.__version__,
        }
