"""WASAPI speaker loopback + optional microphone, written incrementally to WAV."""
from contextlib import ExitStack
import math
from pathlib import Path
import sys
import threading
import wave

import numpy as np

RATE = 16000


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


class WindowsCapture:
    def __init__(self, microphone=True):
        self.microphone = microphone
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def record(self, path: Path, on_chunk, on_ready=lambda: None):
        if sys.platform != "win32":
            raise RuntimeError("Live capture needs Windows 10/11 and the desktop extra. File import works on other systems.")
        import pyaudiowpatch as pa
        with ExitStack() as stack:
            audio = pa.PyAudio()
            stack.callback(audio.terminate)
            speaker = audio.get_default_wasapi_loopback()
            devices = [speaker]
            if self.microphone:
                devices.append(audio.get_default_input_device_info())
            streams = []
            for device in devices:
                rate = int(device["defaultSampleRate"])
                channels = min(2, int(device["maxInputChannels"]))
                if channels < 1:
                    raise RuntimeError(f"No input channels for {device['name']}")
                stream = audio.open(format=pa.paInt16, channels=channels, rate=rate, input=True,
                                    input_device_index=int(device["index"]), frames_per_buffer=rate // 10)
                stack.callback(stream.close)
                streams.append((stream, channels, rate))
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(RATE)
                pending = []
                count = 0
                offset = 0.0
                on_ready()
                while not self.stop_event.is_set():
                    blocks = [mono_pcm(stream.read(rate // 10, exception_on_overflow=True), channels, rate)
                              for stream, channels, rate in streams]
                    block = mix(blocks[0], blocks[1] if len(blocks) > 1 else None)
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
