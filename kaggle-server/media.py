"""Image and audio decoding for the inference node (numpy + Pillow + stdlib only).

Audio deliberately avoids torchaudio, librosa, and scipy: pinning torch 2.3.1
and ``numpy<2`` on Kaggle can leave those preinstalled packages ABI-broken.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Final

import numpy as np
from PIL import Image, UnidentifiedImageError

from omnisight_contracts import MAX_AUDIO_DURATION_MS, MAX_IMAGE_DIMENSION, AudioPayload, ImagePayload

from engine_api import InvalidInputError

ASR_SAMPLE_RATE: Final[int] = 16_000
DURATION_TOLERANCE_MS: Final[int] = 250
_FIR_TAPS: Final[int] = 101


class AudioDecodeError(InvalidInputError):
    """The audio clip could not be decoded or does not match its declared metadata."""


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def load_image(payload: ImagePayload) -> Image.Image:
    """Decode a validated ``ImagePayload`` into an RGB image.

    Rejects images whose real dimensions differ from the declared ones, which
    catches client bugs that would otherwise skew the vision-token budget.
    """
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_DIMENSION * MAX_IMAGE_DIMENSION
    try:
        with Image.open(io.BytesIO(payload.decoded_bytes())) as image:
            image.load()
            if image.size != (payload.width, payload.height):
                raise InvalidInputError(
                    "declared image size does not match the decoded image",
                    details=[
                        f"declared={payload.width}x{payload.height}",
                        f"actual={image.size[0]}x{image.size[1]}",
                    ],
                )
            return image.convert("RGB")
    except InvalidInputError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise InvalidInputError("image bytes could not be decoded", details=[str(exc)]) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedAudio:
    samples: np.ndarray  # float32, mono, range [-1, 1]
    sample_rate: int

    @property
    def duration_ms(self) -> float:
        return 1000.0 * self.samples.shape[0] / self.sample_rate


def decode_wav(data: bytes) -> DecodedAudio:
    """Decode an integer-PCM WAV file (8/16/24/32-bit, any channel count) to mono float32."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioDecodeError(
            "audio must be an uncompressed integer-PCM WAV file", details=[str(exc)]
        ) from exc

    if channels < 1 or rate <= 0:
        raise AudioDecodeError("WAV header is invalid", details=[f"channels={channels}", f"rate={rate}"])
    frame_bytes = channels * width
    usable = len(frames) - len(frames) % frame_bytes
    raw = frames[:usable]

    if width == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        triplets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        pcm = values.astype(np.float32) / 8388608.0
    elif width == 4:
        pcm = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioDecodeError(f"unsupported WAV sample width: {width} bytes")

    if pcm.size == 0:
        raise AudioDecodeError("WAV file contains no audio frames")
    mono = pcm.reshape(-1, channels).mean(axis=1) if channels > 1 else pcm
    return DecodedAudio(samples=np.clip(mono, -1.0, 1.0).astype(np.float32), sample_rate=rate)


def _lowpass_kernel(cutoff: float, taps: int = _FIR_TAPS) -> np.ndarray:
    """Hann-windowed sinc low-pass FIR; ``cutoff`` in cycles/sample (0 < cutoff < 0.5)."""
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.hanning(taps)
    return kernel / kernel.sum()


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample mono audio; low-pass filters first when downsampling to avoid aliasing."""
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    signal = np.asarray(samples, dtype=np.float64)
    if source_rate == target_rate or signal.size == 0:
        return signal.astype(np.float32)
    if target_rate < source_rate:
        # 0.9 of the new Nyquist leaves room for the filter's transition band.
        cutoff = 0.5 * (target_rate / source_rate) * 0.9
        # Full convolution + centered slice keeps len(signal) even when the clip is
        # shorter than the kernel (mode="same" would return the kernel length).
        full = np.convolve(signal, _lowpass_kernel(cutoff))
        start = (_FIR_TAPS - 1) // 2
        signal = full[start : start + signal.size]
    target_length = max(1, int(round(signal.size * target_rate / source_rate)))
    positions = np.arange(target_length, dtype=np.float64) * (source_rate / target_rate)
    resampled = np.interp(positions, np.arange(signal.size, dtype=np.float64), signal)
    return np.clip(resampled, -1.0, 1.0).astype(np.float32)


def prepare_for_asr(payload: AudioPayload) -> np.ndarray:
    """Decode, validate, and resample a push-to-talk clip to 16 kHz mono float32."""
    decoded = decode_wav(payload.decoded_bytes())
    if decoded.sample_rate != payload.sample_rate:
        raise AudioDecodeError(
            "declared sample_rate does not match the WAV header",
            details=[f"declared={payload.sample_rate}", f"header={decoded.sample_rate}"],
        )
    actual_ms = decoded.duration_ms
    if abs(actual_ms - payload.duration_ms) > DURATION_TOLERANCE_MS:
        raise AudioDecodeError(
            "declared duration_ms does not match the audio length",
            details=[f"declared={payload.duration_ms}", f"actual={actual_ms:.0f}"],
        )
    if actual_ms > MAX_AUDIO_DURATION_MS + DURATION_TOLERANCE_MS:
        raise AudioDecodeError(f"audio longer than {MAX_AUDIO_DURATION_MS} ms")
    return resample(decoded.samples, decoded.sample_rate, ASR_SAMPLE_RATE)
