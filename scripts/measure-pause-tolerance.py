"""Where does audio.pause_threshold start cutting real speech short?

Run with: uv run --no-sync python scripts/measure-pause-tolerance.py

Renders sentences through the same SAPI voice JARVIS speaks with, scores every
frame with the real detector, and replays that through the real PhraseEnd rule.
A phrase that ends before the speech does is a mid-sentence cut.
"""

import math
import tempfile
from pathlib import Path

import numpy as np
import speech_recognition as sr

from jarvis.config import AudioConfig
from jarvis.microphone import PhraseEnd
from jarvis.vad import SAMPLE_RATE, SAMPLES, SECONDS_PER_BUFFER, build_detector

PER = SECONDS_PER_BUFFER
OUT = Path(tempfile.mkdtemp(prefix="jarvis-pause-"))
PAUSES = (1.2, 1.5)
FRACTIONS = (1.0, 0.9, 0.85, 0.8, 0.75)

# Deliberately awkward: commas, a trailing clause, an "um", a list. The pauses
# inside these are what a too-short threshold would end the phrase on.
SENTENCES = [
    "Jarvis, can you check the config file and tell me what the whisper model is set to?",
    "So, um, what I want you to do is, open the launcher, and then change the temperature.",
    "Right. The three things I need are the log level, the port, and, uh, the pause threshold.",
    "Can you drive from Oakleigh station to Chadstone, and how long would that take?",
    "No, wait. Not that one. The other one, the one in the config directory.",
]


def render(text: str, path: str) -> None:
    import comtypes.client

    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    fmt.Type = 22  # SAFT16kHz16BitMono, so no resampling is needed
    stream.Format = fmt
    stream.Open(path, 3, False)  # SSFMCreateForWrite
    voice.AudioOutputStream = stream
    voice.Rate = 1  # roughly the configured 210 wpm
    voice.Speak(text, 0)
    stream.Close()


def frames(path: str) -> list[bytes]:
    with sr.AudioFile(path) as source:
        audio = sr.Recognizer().record(source)
    raw = audio.get_raw_data(convert_rate=SAMPLE_RATE, convert_width=2)
    spoken = np.frombuffer(raw, dtype=np.int16)
    # Rendered files stop almost dead on the last word, so a pause never
    # completes. Real silence after it is what makes the margin mean anything.
    samples = np.concatenate([spoken, np.zeros(SAMPLE_RATE * 3, dtype=np.int16)])
    step = SAMPLES
    return [samples[i : i + step].tobytes() for i in range(0, len(samples) - step + 1, step)]


def trace(path: str) -> tuple[list[bool], int]:
    """Quiet per frame under the shipped detector, and the last speaking frame."""
    detector = build_detector(AudioConfig())
    quiet, last_speech = [], 0
    for index, frame in enumerate(frames(path)):
        speech = detector.is_speech(frame)
        quiet.append(not speech)
        if speech:
            last_speech = index
    return quiet, last_speech


def ends_at(quiet: list[bool], fraction: float, pause: float) -> int | None:
    end = PhraseEnd(PER, pause, fraction)
    started = False
    for index, is_quiet in enumerate(quiet):
        if not started:
            started = not is_quiet
            continue
        if end.feed(is_quiet):
            return index
    return None


traces = {}
for i, sentence in enumerate(SENTENCES):
    path = str(OUT / f"say{i}.wav")
    render(sentence, path)
    traces[i] = trace(path)

for pause in PAUSES:
    needed = math.ceil(pause / PER)
    print(f"\n=== pause_threshold {pause}s ({needed} frames of non-speech needed) ===")
    header = "  ".join(f"f={f:<4}" for f in FRACTIONS)
    print(f"{'speech':>7}  {header}")
    for i in sorted(traces):
        quiet, last_speech = traces[i]
        row = []
        for fraction in FRACTIONS:
            index = ends_at(quiet, fraction, pause)
            if index is None:
                row.append("  none")
            else:
                margin = (index - last_speech) * PER
                row.append(f"{margin:+6.2f}" if margin > 0 else f"{margin:+6.2f}*")
        print(f"{last_speech * PER:>6.2f}s  " + "  ".join(f"{cell:<6}" for cell in row))
    print("  margin = seconds between the last speech and the phrase ending. * = cut mid sentence.")
