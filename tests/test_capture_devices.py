"""Exercise the actual capture writer with fake WASAPI streams, without a GPU or audio hardware."""
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from coordy.desktop.capture import WindowsCapture, device_catalog, select_device

HEADSET = 'Headphones (Meeting headset) [Loopback]'
MONITOR = 'Speakers (Monitor) [Loopback]'
MIC = 'Microphone (USB)'


class Audio:
    def __init__(self):
        self.devices = [
            dict(index=1, name=MONITOR, isLoopbackDevice=True, maxInputChannels=2, defaultSampleRate=48000),
            dict(index=2, name=HEADSET, isLoopbackDevice=True, maxInputChannels=6, defaultSampleRate=48000),
            dict(index=3, name=MIC, isLoopbackDevice=False, maxInputChannels=1, defaultSampleRate=44100),
        ]
        self.opened = []
        self.streams = []
        self.terminated = False

    def get_loopback_device_info_generator(self):
        return iter(self.devices[:2])

    def get_device_info_generator_by_host_api(self, **kwargs):
        return iter(self.devices)

    def get_default_wasapi_loopback(self):
        return self.devices[0]

    def get_default_wasapi_device(self, **kwargs):
        return self.devices[2]

    def open(self, **kwargs):
        self.opened.append(kwargs)
        index = kwargs['input_device_index']
        frequency, amplitude = {1: (440, 0), 2: (440, .5), 3: (880, .3)}[index]
        position = 0
        stream = SimpleNamespace(closed=False)
        def read(frames, **options):
            nonlocal position
            phase = np.arange(position, position + frames) / kwargs['rate']
            signal = np.sin(2 * np.pi * frequency * phase) * amplitude
            position += frames
            return np.repeat((signal * 32767).astype('<i2')[:, None], kwargs['channels'], axis=1).tobytes()
        stream.read = read
        stream.close = lambda: setattr(stream, 'closed', True)
        self.streams.append(stream)
        return stream

    def terminate(self):
        self.terminated = True


def run_capture(tmp_path, speaker, microphone, ticks=1):
    audio = Audio()
    levels, devices, chunks = [], [], []
    capture = None
    def on_levels(value):
        levels.append(value)
        if len(levels) >= ticks:
            capture.stop()
    capture = WindowsCapture(microphone, speaker, MIC, on_levels=on_levels, on_devices=devices.append)
    output = tmp_path / 'recording.wav'
    capture._record(output, lambda samples, offset: chunks.append((samples, offset)), lambda: None,
                    SimpleNamespace(PyAudio=lambda: audio, paInt16=8))
    with wave.open(str(output)) as wav:
        assert wav.getframerate() == 16000 and wav.getnchannels() == 1
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(float)
    assert audio.terminated and all(stream.closed for stream in audio.streams)
    return audio, levels, devices, samples


@pytest.mark.parametrize('microphone', [False, True])
def test_selected_meeting_output_is_in_recording_even_without_microphone(tmp_path, microphone):
    audio, levels, devices, samples = run_capture(tmp_path, HEADSET, microphone)
    assert audio.opened[0]['input_device_index'] == 2
    assert audio.opened[0]['channels'] == 6  # Native surround channel count.
    assert len(audio.opened) == (2 if microphone else 1)
    frequencies = np.fft.rfftfreq(len(samples), 1 / 16000)
    spectrum = np.abs(np.fft.rfft(samples))
    assert spectrum[np.argmin(abs(frequencies - 440))] > 1_000_000
    if microphone:
        assert spectrum[np.argmin(abs(frequencies - 880))] > 1_000_000
        assert levels[0]['microphone'] > 0
    else:
        assert levels[0]['microphone'] is None
    assert levels[0]['computer'] > 0
    assert devices[0]['computer'] == HEADSET


def test_mic_activity_does_not_hide_wrong_or_silent_computer_output(tmp_path):
    _, levels, devices, _ = run_capture(tmp_path, MONITOR, True, ticks=20)
    assert levels[-1]['computer'] == 0
    assert levels[-1]['microphone'] > 0
    assert levels[-1]['computer_silent_seconds'] >= 10
    assert devices[0]['computer'] == MONITOR


def test_missing_saved_output_does_not_silently_fall_back():
    audio = Audio()
    with pytest.raises(RuntimeError, match='Choose it again in Settings'):
        select_device(audio, 'Unplugged headset', 'speakers')
    assert not audio.opened
    assert [item['name'] for item in device_catalog(audio)['microphones']] == [MIC]


def test_default_computer_source_cannot_be_a_microphone():
    audio = Audio()
    audio.get_default_wasapi_loopback = lambda: audio.devices[2]
    with pytest.raises(RuntimeError, match='not a microphone'):
        select_device(audio, '', 'speakers')
