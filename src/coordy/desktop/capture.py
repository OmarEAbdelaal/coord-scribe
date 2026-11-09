"""WASAPI speaker loopback + optional microphone, written incrementally to WAV."""
from contextlib import ExitStack
import math
from pathlib import Path
import sys
import threading
import wave

import numpy as np

RATE = 16000
WASAPI = 13


def mono_pcm(data: bytes, channels: int, source_rate: int) -> np.ndarray:
    from scipy.signal import resample_poly
    samples = np.frombuffer(data, dtype=np.int16).reshape(-1, channels).astype(np.float32).mean(axis=1) / 32768
    if source_rate != RATE:
        divisor = math.gcd(source_rate, RATE)
        samples = resample_poly(samples, RATE // divisor, source_rate // divisor)
    return samples.astype(np.float32)


def mix(speaker: np.ndarray, microphone: np.ndarray | None) -> np.ndarray:
    if microphone is None:
        return speaker
    # Leave headroom when both sources speak; keep both streams at equal length.
    if len(microphone) != len(speaker):
        raise RuntimeError("Audio devices returned mismatched blocks")
    return np.clip((speaker + microphone) * 0.5, -1, 1)


def meter_level(peak):
    """Map -60..0 dBFS to a readable 0..100 meter (not post-mix gain)."""
    return 0 if peak <= 0.001 else min(100, round((20 * math.log10(peak) + 60) * 100 / 60))


def device_catalog(audio):
    """Enumerate only real WASAPI loopbacks and physical WASAPI inputs."""
    speakers = list(audio.get_loopback_device_info_generator())
    microphones = [device for device in audio.get_device_info_generator_by_host_api(host_api_type=WASAPI)
                   if int(device["maxInputChannels"]) > 0 and not device.get("isLoopbackDevice", False)]
    return {"speakers": speakers, "microphones": microphones}


def list_audio_devices():
    if sys.platform != "win32":
        return {"speakers": [], "microphones": []}
    import pyaudiowpatch as pa
    audio = pa.PyAudio()
    try:
        catalog = device_catalog(audio)
        return {kind: [{"name": device["name"]} for device in devices] for kind, devices in catalog.items()}
    finally:
        audio.terminate()


def select_device(audio, name, kind):
    if name:
        matches = [device for device in device_catalog(audio)[kind] if device["name"] == name]
        if len(matches) != 1:
            label = "meeting audio output" if kind == "speakers" else "microphone"
            raise RuntimeError(f"Selected {label} is unavailable or ambiguous: {name}. Choose it again in Settings. If names are duplicated, rename the device in Windows Sound settings.")
        device = matches[0]
    else:
        device = audio.get_default_wasapi_loopback() if kind == "speakers" else audio.get_default_wasapi_device(d_in=True)
    if kind == "speakers" and not device.get("isLoopbackDevice", False):
        raise RuntimeError("Computer sound requires a WASAPI playback loopback device, not a microphone.")
    if int(device["maxInputChannels"]) < 1:
        raise RuntimeError(f"No capture channels for {device['name']}")
    return device


class WindowsCapture:
    def __init__(self, microphone=True, speaker_device="", microphone_device="", *,
                 on_levels=lambda levels: None, on_devices=lambda devices: None):
        self.microphone = microphone
        self.speaker_device = speaker_device
        self.microphone_device = microphone_device
        self.on_levels = on_levels
        self.on_devices = on_devices
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def record(self, path: Path, on_chunk, on_ready=lambda: None):
        if sys.platform != "win32":
            raise RuntimeError("Live capture needs Windows 10/11 and the desktop extra. File import works on other systems.")
        import pyaudiowpatch as pa
        self._record(path, on_chunk, on_ready, pa)

    def _record(self, path, on_chunk, on_ready, pa):
        with ExitStack() as stack:
            audio = pa.PyAudio()
            stack.callback(audio.terminate)
            speaker = select_device(audio, self.speaker_device, "speakers")
            devices = [speaker]
            if self.microphone:
                devices.append(select_device(audio, self.microphone_device, "microphones"))
            streams = []
            for device in devices:
                rate = int(device["defaultSampleRate"])
                # Open the endpoint's native channel count, including surround outputs.
                channels = int(device["maxInputChannels"])
                stream = audio.open(format=pa.paInt16, channels=channels, rate=rate, input=True,
                                    input_device_index=int(device["index"]), frames_per_buffer=rate // 10)
                stack.callback(stream.close)
                streams.append((stream, channels, rate))
            self.on_devices({"computer": speaker["name"],
                             "microphone": devices[1]["name"] if self.microphone else None})
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(RATE)
                pending = []
                count = 0
                offset = 0.0
                ticks = 0
                silent_frames = 0
                speaker_peak = mic_peak = 0.0
                on_ready()
                while not self.stop_event.is_set():
                    blocks = [mono_pcm(stream.read(rate // 10, exception_on_overflow=True), channels, rate)
                              for stream, channels, rate in streams]
                    block = mix(blocks[0], blocks[1] if len(blocks) > 1 else None)
                    # Measure the computer source before mixing so the mic cannot mask silence.
                    peak = float(np.max(np.abs(blocks[0]))) if len(blocks[0]) else 0.0
                    speaker_peak = max(speaker_peak, peak)
                    silent_frames = silent_frames + len(blocks[0]) if peak < 0.001 else 0
                    if self.microphone:
                        mic_peak = max(mic_peak, float(np.max(np.abs(blocks[1]))))
                    ticks += 1
                    if ticks % 5 == 0:
                        self.on_levels({"computer": meter_level(speaker_peak),
                                        "microphone": meter_level(mic_peak) if self.microphone else None,
                                        "computer_silent_seconds": silent_frames / RATE})
                        speaker_peak = mic_peak = 0.0
                    output.writeframes((np.clip(block, -1, 1) * 32767).astype('<i2').tobytes())
                    pending.append(block)
                    count += len(block)
                    if count >= RATE * 10:
                        chunk = np.concatenate(pending)
                        on_chunk(chunk, offset)
                        offset += len(chunk) / RATE
                        pending, count = [], 0
                if count:
                    on_chunk(np.concatenate(pending), offset)
