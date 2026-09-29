"""Push-to-talk microphone capture into an in-memory ring buffer.

``AudioRecorder.start_recording()`` opens a 16 kHz, 16-bit mono
``sounddevice.RawInputStream``; PortAudio calls ``_on_audio`` on its own
thread and the samples go into a preallocated 15 s ring buffer (the oldest
audio is overwritten if the key is held longer). ``stop_recording()`` closes
the stream, trims leading/trailing silence, applies RMS normalization with a
peak limit, and returns an in-memory WAV file.
"""

from __future__ import annotations

import io
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from core.logger import get_logger

logger = get_logger("capture.audio")

SAMPLE_RATE: Final[int] = 16_000
SAMPLE_WIDTH: Final[int] = 2  # int16
MAX_SECONDS: Final[float] = 15.0
FRAME_MS: Final[int] = 20
PAD_MS: Final[int] = 150
MIN_SPEECH_MS: Final[int] = 300
ABSOLUTE_SILENCE_DBFS: Final[float] = -50.0
NOISE_MARGIN_DB: Final[float] = 10.0
TARGET_RMS_DBFS: Final[float] = -20.0
MAX_GAIN_DB: Final[float] = 24.0
PEAK_CEILING: Final[float] = 0.97


class AudioDeviceError(RuntimeError):
    """The microphone could not be opened or failed mid-recording."""


class NoSpeechError(RuntimeError):
    """The clip held no speech after silence trimming."""


@dataclass(frozen=True)
class RecordingResult:
    wav_bytes: bytes
    sample_rate: int
    duration_ms: int
    raw_duration_ms: int
    gain_db: float
    speech_rms_dbfs: float
    overflows: int = 0


def _dbfs(value: float) -> float:
    return 20.0 * float(np.log10(max(value, 1e-9)))


def process_pcm(pcm: bytes, sample_rate: int = SAMPLE_RATE, overflows: int = 0) -> RecordingResult:
    """Trim silence, normalize loudness, and wrap int16 mono PCM as WAV bytes.

    Raises ``NoSpeechError`` when fewer than ``MIN_SPEECH_MS`` of speech remain.
    """
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    raw_ms = int(round(1000 * samples.size / sample_rate))
    frame = max(1, sample_rate * FRAME_MS // 1000)
    usable = samples.size - samples.size % frame
    if usable < frame:
        raise NoSpeechError("the recording is empty")
    frames = samples[:usable].reshape(-1, frame)
    frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
    frame_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-9))
    noise_floor = float(np.percentile(frame_db, 10))
    threshold = max(ABSOLUTE_SILENCE_DBFS, noise_floor + NOISE_MARGIN_DB)
    voiced = np.flatnonzero(frame_db > threshold)
    if voiced.size == 0:
        raise NoSpeechError("no speech detected (only silence or steady background noise)")

    pad_frames = PAD_MS // FRAME_MS
    first = max(0, int(voiced[0]) - pad_frames) * frame
    last = min(frames.shape[0], int(voiced[-1]) + 1 + pad_frames) * frame
    clip = samples[first:last]
    duration_ms = int(round(1000 * clip.size / sample_rate))
    if duration_ms < MIN_SPEECH_MS:
        raise NoSpeechError(f"speech too short ({duration_ms} ms)")

    speech_rms = float(np.sqrt(np.mean(frames[voiced] ** 2)))
    peak = float(np.max(np.abs(clip))) or 1e-9
    gain = 10 ** ((TARGET_RMS_DBFS - _dbfs(speech_rms)) / 20.0)
    gain = min(gain, 10 ** (MAX_GAIN_DB / 20.0), PEAK_CEILING / peak)
    normalized = np.clip(clip * gain, -1.0, 1.0)
    pcm_out = (normalized * 32767.0).astype("<i2").tobytes()

    buffer = io.BytesIO()
    try:
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(SAMPLE_WIDTH)
            writer.setframerate(sample_rate)
            writer.writeframes(pcm_out)
        wav = buffer.getvalue()
    finally:
        buffer.close()
    return RecordingResult(
        wav_bytes=wav,
        sample_rate=sample_rate,
        duration_ms=duration_ms,
        raw_duration_ms=raw_ms,
        gain_db=round(_dbfs(gain), 1),
        speech_rms_dbfs=round(_dbfs(speech_rms), 1),
        overflows=overflows,
    )


class AudioRecorder:
    """Push-to-talk recorder backed by a fixed-size ring buffer."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, max_seconds: float = MAX_SECONDS, device: Any = None) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self._capacity = int(sample_rate * max_seconds) * SAMPLE_WIDTH
        self._ring = bytearray(self._capacity)
        self._write = 0
        self._filled = 0
        self._overflows = 0
        self._lock = threading.Lock()
        self._stream: Any = None
        self._started_at = 0.0

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._stream is not None

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000.0 if self.is_recording else 0.0

    def _on_audio(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """PortAudio callback (audio thread): copy samples into the ring."""
        chunk = bytes(indata)
        with self._lock:
            if status:
                self._overflows += 1
            size = len(chunk)
            if size >= self._capacity:
                chunk = chunk[-self._capacity :]
                size = self._capacity
            end = self._write + size
            if end <= self._capacity:
                self._ring[self._write : end] = chunk
            else:
                split = self._capacity - self._write
                self._ring[self._write :] = chunk[:split]
                self._ring[: size - split] = chunk[split:]
            self._write = end % self._capacity
            self._filled = min(self._capacity, self._filled + size)

    def start_recording(self) -> None:
        """Open the microphone and start filling the ring buffer."""
        import sounddevice as sd

        with self._lock:
            if self._stream is not None:
                return
            self._write = 0
            self._filled = 0
            self._overflows = 0
        try:
            stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.device,
                callback=self._on_audio,
            )
            stream.start()
        except (sd.PortAudioError, ValueError, OSError) as exc:
            hint = ""
            if "Unanticipated host error" in str(exc) or "Invalid device" in str(exc):
                hint = (
                    " - check Windows Settings > Privacy & security > Microphone: both "
                    "'Microphone access' and 'Let desktop apps access your microphone' must be on"
                )
            raise AudioDeviceError(f"microphone unavailable: {exc}{hint}") from exc
        with self._lock:
            self._stream = stream
        self._started_at = time.monotonic()
        logger.info("recording started (%s)", stream.device if hasattr(stream, "device") else "default device")

    def _snapshot(self) -> bytes:
        with self._lock:
            if self._filled < self._capacity:
                data = bytes(self._ring[: self._filled])
            else:
                data = bytes(self._ring[self._write :] + self._ring[: self._write])
            return data

    def stop_recording(self) -> RecordingResult:
        """Stop the stream and return the trimmed, normalized clip as WAV bytes."""
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is None:
            raise NoSpeechError("recording was not started")
        try:
            stream.stop()
        finally:
            stream.close()
        pcm = self._snapshot()
        with self._lock:
            overflows = self._overflows
        result = process_pcm(pcm, self.sample_rate, overflows)
        logger.info(
            "recording stopped: %d ms raw -> %d ms speech, gain %+.1f dB, %d overflow(s)",
            result.raw_duration_ms,
            result.duration_ms,
            result.gain_db,
            overflows,
        )
        return result

    def cancel(self) -> None:
        """Stop without processing (used on shutdown or when a capture is abandoned)."""
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def close(self) -> None:
        self.cancel()
