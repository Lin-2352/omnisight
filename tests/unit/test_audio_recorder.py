"""Push-to-talk audio: ring buffer, PCM processing and the Windows microphone consent check.

``sounddevice`` and ``winreg`` are replaced with in-memory fakes (both are imported
lazily inside ``capture.audio``), so no microphone or registry state is needed and
nothing is written anywhere.
"""

from __future__ import annotations

import io
import sys
import types
import wave
from typing import Any

import numpy as np
import pytest

from capture import audio
from capture.audio import (
    MAX_GAIN_DB,
    PEAK_CEILING,
    SAMPLE_RATE,
    AudioDeviceError,
    AudioRecorder,
    MicrophonePermissionError,
    NoSpeechError,
    microphone_permission,
    process_pcm,
)

RATE = SAMPLE_RATE


def pcm(signal: np.ndarray) -> bytes:
    return (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def tone(seconds: float, amplitude: float = 0.03, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return amplitude * np.sin(2 * np.pi * freq * t)


def noise(seconds: float, level: float = 0.001, seed: int = 3) -> np.ndarray:
    return np.random.default_rng(seed).normal(0, level, int(seconds * RATE))


def spoken(signal: np.ndarray, pad_s: float = 0.5) -> np.ndarray:
    """Surround ``signal`` with room noise: the trimmer calibrates its floor on the quiet frames."""
    return np.concatenate([noise(pad_s, seed=1), signal + noise(signal.size / RATE, seed=2), noise(pad_s, seed=4)])


def read_wav(data: bytes) -> tuple[wave._wave_params, np.ndarray]:
    with wave.open(io.BytesIO(data), "rb") as reader:
        params = reader.getparams()
        samples = np.frombuffer(reader.readframes(params.nframes), dtype="<i2")
    return params, samples


# ---------------------------------------------------------------------------
# process_pcm
# ---------------------------------------------------------------------------


def test_silence_is_trimmed_to_the_speech_plus_padding() -> None:
    signal = noise(3.0)
    signal[RATE : 2 * RATE] += tone(1.0)
    result = process_pcm(pcm(signal))
    assert result.raw_duration_ms == 3000
    assert 1000 <= result.duration_ms <= 1400  # 1 s of speech + up to 150 ms padding per side
    assert result.gain_db > 0


def test_output_is_a_16_khz_mono_int16_wav() -> None:
    result = process_pcm(pcm(spoken(tone(1.0))))
    params, samples = read_wav(result.wav_bytes)
    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, RATE)
    assert samples.size == params.nframes > 0
    assert result.sample_rate == RATE


def test_loudness_is_normalized_but_never_clips() -> None:
    loud = np.sign(tone(1.0, amplitude=1.0))  # full-scale square wave
    result = process_pcm(pcm(spoken(loud)))
    _, samples = read_wav(result.wav_bytes)
    assert np.max(np.abs(samples)) <= PEAK_CEILING * 32767 + 1


def test_gain_is_capped_for_very_quiet_speech() -> None:
    signal = np.concatenate([np.zeros(RATE // 2), tone(1.0, amplitude=0.006), np.zeros(RATE // 2)])
    result = process_pcm(pcm(signal))
    assert result.gain_db <= MAX_GAIN_DB + 0.1


@pytest.mark.parametrize(
    ("signal", "message"),
    [
        (np.zeros(0), "empty"),
        (noise(2.0, level=0.002), "no speech"),
        # 20 ms blip at the very start of a 120 ms clip: even with padding it stays under 300 ms
        (np.concatenate([tone(0.02, amplitude=0.3), np.zeros(int(0.1 * RATE))]), "too short"),
    ],
    ids=["empty", "noise-only", "too-short"],
)
def test_clips_without_usable_speech_raise(signal: np.ndarray, message: str) -> None:
    with pytest.raises(NoSpeechError, match=message):
        process_pcm(pcm(signal))


# ---------------------------------------------------------------------------
# Recorder with a fake PortAudio stream
# ---------------------------------------------------------------------------


class FakePortAudioError(Exception):
    pass


class FakeStream:
    instances: list[FakeStream] = []
    fail_with: Exception | None = None

    def __init__(self, *, samplerate: int, channels: int, dtype: str, device: Any, callback: Any) -> None:
        if FakeStream.fail_with is not None:
            raise FakeStream.fail_with
        self.samplerate, self.channels, self.dtype, self.device = samplerate, channels, dtype, device
        self.callback = callback
        self.started = self.stopped = self.closed = False
        FakeStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def feed(self, data: bytes, status: int = 0) -> None:
        self.callback(data, len(data) // 2, None, status)


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    FakeStream.instances = []
    FakeStream.fail_with = None
    module = types.SimpleNamespace(RawInputStream=FakeStream, PortAudioError=FakePortAudioError)
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    monkeypatch.setattr(audio, "microphone_permission", lambda: "allowed")
    return module


def test_recorder_opens_a_16khz_int16_mono_stream(fake_sd: types.SimpleNamespace) -> None:
    recorder = AudioRecorder()
    recorder.start_recording()
    stream = FakeStream.instances[-1]
    assert (stream.samplerate, stream.channels, stream.dtype, stream.started) == (RATE, 1, "int16", True)
    assert recorder.is_recording and recorder.elapsed_ms >= 0
    recorder.start_recording()  # a second press while recording is a no-op
    assert len(FakeStream.instances) == 1
    recorder.cancel()
    assert stream.stopped and stream.closed and not recorder.is_recording


def test_recording_round_trip_returns_processed_speech(fake_sd: types.SimpleNamespace) -> None:
    recorder = AudioRecorder()
    recorder.start_recording()
    signal = np.concatenate([noise(0.5), tone(1.0) + noise(1.0), noise(0.5)])
    data = pcm(signal)
    stream = FakeStream.instances[-1]
    for start in range(0, len(data), 640):  # 20 ms blocks like PortAudio
        stream.feed(data[start : start + 640])
    result = recorder.stop_recording()
    assert 1000 <= result.duration_ms <= 1400
    assert result.overflows == 0
    assert stream.closed


def test_ring_buffer_keeps_only_the_most_recent_audio(fake_sd: types.SimpleNamespace) -> None:
    recorder = AudioRecorder(max_seconds=1.0)
    recorder.start_recording()
    stream = FakeStream.instances[-1]
    ramp = (np.arange(int(1.5 * RATE)) % 30000).astype("<i2")
    data = ramp.tobytes()
    for start in range(0, len(data), 1000):
        stream.feed(data[start : start + 1000])
    snapshot = np.frombuffer(recorder._snapshot(), dtype="<i2")
    assert snapshot.size == RATE  # capacity: 1 s
    assert np.array_equal(snapshot, ramp[-RATE:])  # the last second, in order
    recorder.cancel()


def test_a_single_oversized_callback_block_is_truncated_to_capacity(fake_sd: types.SimpleNamespace) -> None:
    recorder = AudioRecorder(max_seconds=0.5)
    recorder.start_recording()
    block = (np.arange(RATE) % 1000).astype("<i2")
    FakeStream.instances[-1].feed(block.tobytes())
    snapshot = np.frombuffer(recorder._snapshot(), dtype="<i2")
    assert np.array_equal(snapshot, block[-RATE // 2 :])
    recorder.cancel()


def test_overflow_statuses_are_counted(fake_sd: types.SimpleNamespace) -> None:
    recorder = AudioRecorder()
    recorder.start_recording()
    stream = FakeStream.instances[-1]
    data = pcm(spoken(tone(1.0, amplitude=0.2)))
    stream.feed(data[: len(data) // 2], status=1)
    stream.feed(data[len(data) // 2 :], status=1)
    assert recorder.stop_recording().overflows == 2


def test_stop_without_start_raises_no_speech(fake_sd: types.SimpleNamespace) -> None:
    with pytest.raises(NoSpeechError, match="not started"):
        AudioRecorder().stop_recording()
    AudioRecorder().close()  # closing an idle recorder is harmless


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (FakePortAudioError("Error opening RawInputStream: Unanticipated host error [PaErrorCode -9999]"), True),
        (ValueError("Invalid device"), True),
        (OSError("device busy"), False),
    ],
    ids=["host-error", "invalid-device", "os-error"],
)
def test_device_failures_become_audio_device_errors(fake_sd: types.SimpleNamespace, error: Exception, hint: bool) -> None:
    FakeStream.fail_with = error
    with pytest.raises(AudioDeviceError) as info:
        AudioRecorder().start_recording()
    assert "microphone unavailable" in str(info.value)
    assert ("Privacy & security > Microphone" in str(info.value)) is hint
    assert not isinstance(info.value, MicrophonePermissionError)


@pytest.mark.parametrize("permission", ["denied_device", "denied_user", "denied_desktop_apps"])
def test_blocked_microphone_raises_a_permission_error_before_opening_the_device(
    fake_sd: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch, permission: str
) -> None:
    monkeypatch.setattr(audio, "microphone_permission", lambda: permission)
    with pytest.raises(MicrophonePermissionError) as info:
        AudioRecorder().start_recording()
    assert info.value.permission == permission
    assert str(info.value) == audio.PERMISSION_MESSAGES[permission]
    assert "Check again" in str(info.value)
    assert FakeStream.instances == []


# ---------------------------------------------------------------------------
# Consent store (read-only registry) with a fake winreg
# ---------------------------------------------------------------------------


def fake_winreg(values: dict[tuple[str, str], str]) -> types.SimpleNamespace:
    class Key:
        def __init__(self, root: str, path: str) -> None:
            if (root, path) not in values:
                raise OSError(2, "not found")
            self.value = values[(root, path)]

        def __enter__(self) -> Key:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    return types.SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        HKEY_CURRENT_USER="HKCU",
        OpenKey=lambda root, path: Key(root, path),
        QueryValueEx=lambda key, name: (key.value, 1),
    )


KEY = audio._CONSENT_KEY


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, "allowed"),
        ({("HKLM", KEY): "Allow", ("HKCU", KEY): "Allow"}, "allowed"),
        ({("HKLM", KEY): "Deny", ("HKCU", KEY): "Allow"}, "denied_device"),
        ({("HKLM", KEY): "Allow", ("HKCU", KEY): "Deny"}, "denied_user"),
        ({("HKLM", KEY): "Allow", ("HKCU", KEY): "Allow", ("HKCU", KEY + r"\NonPackaged"): "Deny"}, "denied_desktop_apps"),
    ],
    ids=["no-keys", "all-allowed", "device-off", "user-off", "desktop-apps-off"],
)
def test_microphone_permission_reads_the_consent_store(
    monkeypatch: pytest.MonkeyPatch, values: dict[tuple[str, str], str], expected: str
) -> None:
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(values))
    assert microphone_permission() == expected


def test_microphone_permission_is_unknown_without_winreg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "winreg", None)  # makes `import winreg` raise ImportError
    assert microphone_permission() == "unknown"


def test_real_consent_store_is_readable() -> None:
    assert microphone_permission() in {"allowed", "denied_device", "denied_user", "denied_desktop_apps", "unknown"}
