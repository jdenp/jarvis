"""Real speech with footsteps over the pause, through the real capture loop.

Finds the point where loudness alone stops being able to end a phrase.

Run with: uv run --no-sync python scripts/measure-noise-rejection.py
"""

import tempfile
from pathlib import Path

import comtypes.client
import numpy as np
import speech_recognition as sr

from jarvis.config import AudioConfig
from jarvis.microphone import Microphone
from jarvis.vad import SAMPLE_RATE, SAMPLES

OUT = Path(tempfile.mkdtemp(prefix="jarvis-noise-"))
SENTENCE = "Jarvis, what is the whisper model set to in the config file?"


def render(text, path):
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    out = comtypes.client.CreateObject("SAPI.SpFileStream")
    fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    fmt.Type = 22
    out.Format = fmt
    out.Open(path, 3, False)
    voice.AudioOutputStream = out
    voice.Rate = 1
    voice.Speak(text, 0)
    out.Close()


def load(path):
    with sr.AudioFile(path) as src:
        audio = sr.Recognizer().record(src)
    raw = audio.get_raw_data(convert_rate=SAMPLE_RATE, convert_width=2)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def footsteps(seconds, every=0.5, length=0.1):
    """Steps of `length` seconds, one every `every` seconds, as loud as speech."""
    out = np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)
    count = int(SAMPLE_RATE * length)
    step = (np.sin(2 * np.pi * 60 * np.arange(count) / SAMPLE_RATE) * 0.35).astype(np.float32)
    for start in range(0, len(out) - count, int(SAMPLE_RATE * every)):
        out[start : start + count] = step
    return out


class Playback:
    CHUNK, SAMPLE_RATE, SAMPLE_WIDTH = SAMPLES, SAMPLE_RATE, 2

    def __init__(self, samples):
        self.stream = self
        raw = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
        self._buffers = [
            raw[i : i + SAMPLES].tobytes() for i in range(0, len(raw) - SAMPLES + 1, SAMPLES)
        ]
        self._index = 0

    def read(self, _size):
        if self._index >= len(self._buffers):
            return b""
        self._index += 1
        return self._buffers[self._index - 1]


def phrase_length(mode, signal):
    mic = Microphone(AudioConfig(vad=mode, min_energy_threshold=55, phrase_time_limit=60.0))
    mic._running.set()
    mic._run(Playback(signal))
    audio = mic.listen(timeout=0)
    if audio is None:
        return None
    return len(audio.frame_data) / (SAMPLE_RATE * 2)


def show(value):
    return "none" if value is None else f"{value:.2f}s"


wav = str(OUT / "sentence.wav")
render(SENTENCE, wav)
speech = load(wav)
print(f"{len(speech) / SAMPLE_RATE:.1f}s of speech, then 40s of footsteps at varying rates.")
print("Phrase length delivered = how long before the agent hears you.")
print()
print(f"{'steps/sec':>10} {'duty':>6} {'energy':>11} {'silero':>11}")

for every in (0.5, 0.35, 0.25, 0.2, 0.15):
    signal = np.concatenate([speech, footsteps(40.0, every=every)])
    energy = phrase_length("energy", signal)
    silero = phrase_length("silero", signal)
    print(f"{1 / every:>10.1f} {0.1 / every:>5.0%} {show(energy):>11} {show(silero):>11}")
