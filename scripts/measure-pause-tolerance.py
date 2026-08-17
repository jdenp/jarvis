"""How low can audio.pause_quiet_fraction go before it cuts real speech short?

Run with: uv run --no-sync python scripts/measure-pause-tolerance.py


Renders sentences through the same SAPI voice JARVIS speaks with, then replays
the energy trace through the real threshold dynamics and the real PhraseEnd rule.
A phrase that ends before the speech does is a mid-sentence cut.
"""

import math
import tempfile
from pathlib import Path

import speech_recognition as sr

from jarvis.config import AudioConfig
from jarvis.microphone import Microphone, PhraseEnd, buffer_energy

CHUNK, RATE = 1024, 16_000
PER = CHUNK / RATE
OUT = Path(tempfile.mkdtemp(prefix="jarvis-pause-"))

# Deliberately awkward: commas, a trailing clause, an "um", a list. The pauses
# inside these are what a too-low fraction would end the phrase on.
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


def energies(path: str) -> list[float]:
    with sr.AudioFile(path) as source:
        audio = sr.Recognizer().record(source)
    raw = audio.get_raw_data(convert_rate=RATE, convert_width=2)
    frames = [raw[i : i + CHUNK * 2] for i in range(0, len(raw) - CHUNK * 2, CHUNK * 2)]
    return [buffer_energy(f) for f in frames]


def trace(values: list[float]) -> tuple[list[bool], int]:
    """Quiet/loud per buffer under the live threshold, and the last loud buffer."""
    mic = Microphone(AudioConfig())  # dynamic threshold, floor 55, as shipped
    mic._recognizer.energy_threshold = 300.0
    quiet, last_loud = [], 0
    for index, energy in enumerate(values):
        is_quiet = energy <= mic._recognizer.energy_threshold
        quiet.append(is_quiet)
        if not is_quiet:
            last_loud = index
        mic._adjust(energy, PER)
    return quiet, last_loud


def ends_at(quiet: list[bool], fraction: float, pause: float) -> int | None:
    end = PhraseEnd(PER, pause, fraction)
    started = False
    for index, is_quiet in enumerate(quiet):
        if not started:
            started = not is_quiet
            continue
        if end.feed(0.0 if is_quiet else 1e9, 1.0):
            return index
    return None


for pause in (1.5, 1.7):
    print(f"\n=== pause_threshold {pause}s ({math.ceil(pause / PER)} buffers of quiet needed) ===")
    header = "  ".join(f"f={f:<4}" for f in (1.0, 0.9, 0.85, 0.8, 0.75, 0.7))
    print(f"{'speech':>7}  {header}")
    for i, sentence in enumerate(SENTENCES):
        path = str(OUT / f"say{i}.wav")
        render(sentence, path)
        quiet, last_loud = trace(energies(path))
        row = []
        for f in (1.0, 0.9, 0.85, 0.8, 0.75, 0.7):
            index = ends_at(quiet, f, pause)
            if index is None:
                row.append("  none")
            else:
                margin = (index - last_loud) * PER
                row.append(f"{margin:+6.2f}" if margin > 0 else f"{margin:+6.2f}*")
        print(f"{last_loud * PER:>6.2f}s  " + "  ".join(f"{cell:<6}" for cell in row))
    print("  margin = seconds between the last speech and the phrase ending. * = cut mid sentence.")
