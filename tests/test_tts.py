from __future__ import annotations

import threading
import time

import pytest

from jarvis.tts import (
    NullSpeaker,
    SpeechEngine,
    _devices,
    _kokoro_speed,
    _sapi_rate,
    iter_sentences,
)


@pytest.mark.parametrize(
    ("wpm", "expected"),
    [(175, 0), (180, 0), (100, -2), (400, 3), (1, -10), (100_000, 10)],
)
def test_words_per_minute_maps_onto_the_sapi_scale(wpm, expected):
    assert _sapi_rate(wpm) == expected


def test_sapi_rate_never_leaves_the_valid_range():
    assert all(-10 <= _sapi_rate(w) <= 10 for w in (0, 1, 50, 175, 500, 10_000))


def collect(chunks, **kwargs):
    return list(iter_sentences(chunks, **kwargs))


def test_splits_on_sentence_ends():
    assert collect(["Good evening, sir. ", "Everything is nominal here."]) == [
        "Good evening, sir.",
        "Everything is nominal here.",
    ]


def test_token_sized_chunks_are_regrouped():
    tokens = ["Good", " even", "ing,", " sir", ".", " All", " systems", " are", " nominal", "."]
    assert collect(tokens) == ["Good evening, sir.", "All systems are nominal."]


def test_punctuation_inside_quotes_still_ends_the_sentence():
    assert collect(['He said "the reactor is stable." ', "I have my doubts."]) == [
        'He said "the reactor is stable."',
        "I have my doubts.",
    ]


def test_short_fragment_merges_into_the_next_sentence():
    assert collect(["Yes. ", "The reactor is holding at ninety percent."]) == [
        "Yes. The reactor is holding at ninety percent."
    ]


def test_newline_is_a_boundary():
    assert collect(["A rather long first line here\nand a second one"]) == [
        "A rather long first line here",
        "and a second one",
    ]


def test_trailing_text_without_punctuation_is_still_yielded():
    assert collect(["No full stop here"]) == ["No full stop here"]


def test_empty_stream_yields_nothing():
    assert collect([]) == []
    assert collect(["", "   "]) == []


def test_speech_engine_speaks_everything_queued():
    spoken: list[str] = []

    class Recorder(NullSpeaker):
        def speak(self, text: str) -> None:
            spoken.append(text)

    engine = SpeechEngine(Recorder)
    engine.say("First line.")
    engine.say("Second line.")
    engine.say("   ")  # ignored
    assert engine.wait(timeout=5)
    engine.close()
    assert spoken == ["First line.", "Second line."]


def test_wait_does_not_return_early_while_utterances_are_still_queued():
    """The failure this guards against only shows up under interleaving, so
    hold the worker on the first utterance and queue more behind it."""
    import threading

    started = threading.Event()
    release = threading.Event()
    spoken: list[str] = []

    class Slow(NullSpeaker):
        def speak(self, text: str) -> None:
            started.set()
            release.wait(timeout=5)
            spoken.append(text)

    engine = SpeechEngine(Slow)
    engine.say("first")
    assert started.wait(timeout=5)
    engine.say("second")
    assert engine.speaking is True
    release.set()
    assert engine.wait(timeout=5)
    assert spoken == ["first", "second"]
    assert engine.speaking is False
    engine.close()


def test_interrupt_clears_the_queue_and_releases_waiters():
    spoken: list[str] = []

    class Recorder(NullSpeaker):
        def speak(self, text: str) -> None:
            spoken.append(text)

    engine = SpeechEngine(Recorder)
    engine.say("one")
    assert engine.wait(timeout=5)
    engine.interrupt()
    assert engine.wait(timeout=5)
    assert engine.speaking is False
    engine.close()


def test_interrupt_does_not_wait_for_the_utterance_it_is_cancelling():
    """SAPI was spoken synchronously and cancelled with a purge from whichever
    thread called interrupt(). SAPI lives in the apartment that created it, so that
    purge could not be delivered until the Speak it was meant to cancel had
    finished - and it blocked the caller until then. close() paid that on every
    shutdown, waiting out the last reply before it could exit."""
    started = threading.Event()

    class Slow(NullSpeaker):
        """Blocks in speak() until told to stop, as a real backend does."""

        def __init__(self) -> None:
            self.done = threading.Event()

        def speak(self, text: str) -> None:
            started.set()
            assert self.done.wait(timeout=5), "stop() never arrived"

        def stop(self) -> None:
            self.done.set()

    engine = SpeechEngine(Slow)
    engine.say("A long reply that someone is about to talk over.")
    assert started.wait(timeout=5)

    at = time.monotonic()
    engine.interrupt()
    elapsed = time.monotonic() - at

    assert elapsed < 1.0, f"interrupt() blocked the caller for {elapsed:.2f}s"
    assert engine.wait(timeout=5), "and the utterance really was cut short"
    engine.close()


def test_speech_engine_survives_a_broken_backend():
    class Broken(NullSpeaker):
        def speak(self, text: str) -> None:
            raise RuntimeError("no audio device")

    engine = SpeechEngine(Broken)
    engine.say("This will fail.")
    assert engine.wait(timeout=5)
    engine.close()  # must not hang or raise


# ----------------------------------------------------------------------- kokoro


@pytest.mark.parametrize(
    ("wpm", "speed"), [(175, 1.0), (210, 1.2), (88, 0.5), (10, 0.5), (1000, 2.0)]
)
def test_words_per_minute_maps_onto_kokoros_multiplier(wpm, speed):
    assert _kokoro_speed(wpm) == pytest.approx(speed, abs=0.01)


def test_auto_tries_the_gpu_first_and_keeps_the_cpu_behind_it():
    """The same shape as Whisper's: cuda configured but not installed should
    fall through to a working voice with a line in the log."""
    assert _devices("auto") == ["cuda", "cpu"]
    assert _devices("cpu") == ["cpu"]
    assert _devices(" CUDA ") == ["cuda"]


def test_a_missing_model_says_which_file_and_where_it_comes_from(tmp_path, monkeypatch):
    """330MB is not something to download behind somebody's back, so the whole
    of that decision is one clear line rather than a stall."""
    from dataclasses import replace

    from jarvis.config import TtsConfig
    from jarvis.tts import KokoroSpeaker

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    config = replace(TtsConfig(), engine="kokoro")
    with pytest.raises(FileNotFoundError) as raised:
        KokoroSpeaker(config)
    assert "kokoro-v1.0.onnx" in str(raised.value)
    assert "kokoro-onnx/releases" in str(raised.value)


def test_auto_falls_through_to_sapi_when_nothing_has_been_downloaded(tmp_path, monkeypatch):
    """Otherwise adding the backend breaks every machine that has not fetched it."""
    from jarvis.config import TtsConfig
    from jarvis.tts import build_speaker

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    fell_back = []

    class NoSapi(Exception):
        pass

    def refuse(config):
        fell_back.append(config)
        raise NoSapi("no SAPI on this box either")

    monkeypatch.setattr("jarvis.tts.SapiSpeaker", refuse)
    assert isinstance(build_speaker(TtsConfig()), NullSpeaker)
    assert fell_back, "it went looking for sapi rather than raising"


def test_an_unknown_engine_lists_the_ones_there_are():
    from dataclasses import replace

    from jarvis.config import TtsConfig
    from jarvis.tts import build_speaker

    with pytest.raises(ValueError, match="kokoro"):
        build_speaker(replace(TtsConfig(), engine="elevenlabs"))


class FakeKokoro:
    """Synthesis without the 330MB. A second of full scale per call.

    Full scale rather than silence so that silence added around it is
    distinguishable from what was synthesised.
    """

    def __init__(self) -> None:
        self.asked: list[dict] = []

    def create(self, text, voice, speed, lang):
        import numpy as np

        self.asked.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return np.ones(24000, dtype="float32"), 24000


class FakeStream:
    def __init__(self) -> None:
        self.written = 0
        self.data = bytearray()

    def write(self, data):
        self.written += len(data)
        self.data += data

    def stop_stream(self):
        pass

    def close(self):
        pass


def kokoro(monkeypatch, tmp_path, **overrides):
    """A speaker with the files and the model faked out."""
    from dataclasses import replace

    from jarvis.config import TtsConfig
    from jarvis.tts import KokoroSpeaker

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    (tmp_path / "models").mkdir()
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        (tmp_path / "models" / name).write_bytes(b"not really a model")
    monkeypatch.setattr(KokoroSpeaker, "_load", lambda self, model, voices: FakeKokoro())

    speaker = KokoroSpeaker(replace(TtsConfig(), engine="kokoro", **overrides))
    stream = FakeStream()
    monkeypatch.setattr(speaker, "_open", lambda: stream)
    return speaker, stream


def test_a_british_voice_is_read_with_british_phonemes(monkeypatch, tmp_path):
    """Same letters, different sounds. bm_george reading en-us is audible."""
    speaker, _ = kokoro(monkeypatch, tmp_path)
    speaker.speak("Half past two, sir.")
    assert speaker._kokoro.asked[0]["lang"] == "en-gb"
    assert speaker._kokoro.asked[0]["voice"] == "bm_george"


def test_an_american_voice_is_not(monkeypatch, tmp_path):
    speaker, _ = kokoro(monkeypatch, tmp_path, kokoro_voice="am_michael")
    speaker.speak("Half past two, sir.")
    assert speaker._kokoro.asked[0]["lang"] == "en-us"


def test_it_plays_what_was_synthesised(monkeypatch, tmp_path):
    speaker, stream = kokoro(monkeypatch, tmp_path)
    speaker.speak("Half past two, sir.")
    assert stream.written == (24000 + 3600) * 4, "a second of float32 at 24k, behind the lead-in"


def test_the_lead_in_is_silence_and_nothing_synthesised_is_lost(monkeypatch, tmp_path):
    """Kokoro leaves about 200ms of quiet in front of the first phoneme and
    kokoro-onnx trims it off, which leaves 25 to 50ms. A device waking up eats
    that, and "a" and "I" are short enough to go with it."""
    import numpy as np

    speaker, stream = kokoro(monkeypatch, tmp_path)
    speaker.speak("I have opened Spotify.")

    played = np.frombuffer(bytes(stream.data), dtype="float32")
    lead_in = int(24000 * 0.15)
    assert not played[:lead_in].any(), "silence first"
    assert played[lead_in:].all(), "then every sample that was synthesised"


def test_stopping_lands_inside_the_sentence_rather_than_after_it(monkeypatch, tmp_path):
    """Barge-in is the whole reason this is written a chunk at a time - a
    backend that only checks between utterances talks over the interruption."""
    speaker, _ = kokoro(monkeypatch, tmp_path)

    class StopsAfterTheFirstChunk(FakeStream):
        def write(self, data):
            super().write(data)
            speaker.stop()

    stopping = StopsAfterTheFirstChunk()
    monkeypatch.setattr(speaker, "_open", lambda: stopping)
    speaker.speak("A rather long answer nobody wants to sit through.")
    assert stopping.written == 2400 * 4, "one tenth of a second, then it stopped"


def test_the_volume_setting_reaches_the_samples(monkeypatch, tmp_path):
    """Zero is the one volume that has to be applied, and `if volume:` skipped
    it - a mute JARVIS played at full scale."""
    import numpy as np

    speaker, stream = kokoro(monkeypatch, tmp_path, volume=0.0)
    speaker.speak("Quietly, sir.")
    played = np.frombuffer(bytes(stream.data), dtype="float32")
    assert len(played) == 24000 + 3600, "still played"
    assert not played.any(), "just silent"


# ------------------------------------------------------------------- loudness


def test_a_quiet_clip_is_brought_up_to_the_ceiling():
    """Kokoro comes out a long way short of full scale, which is fine on a desk
    speaker a foot away and not on a phone across a room."""
    import numpy as np

    from jarvis.tts import normalised

    # Within the 4x cap, so it actually reaches the ceiling.
    quiet = np.array([0.1, -0.3, 0.15], dtype="float32")
    louder = normalised(np, quiet)
    assert abs(float(np.max(np.abs(louder))) - 0.95) < 1e-5


def test_it_never_goes_over_the_ceiling():
    """Anything above one clips, which is why tts.volume can only attenuate."""
    import numpy as np

    from jarvis.tts import normalised

    loud = np.array([0.9, -1.0, 0.99], dtype="float32")
    assert float(np.max(np.abs(normalised(np, loud)))) <= 0.95 + 1e-5


def test_near_silence_is_not_amplified_into_noise():
    """A clip that is quiet because it is quiet stays quiet."""
    import numpy as np

    from jarvis.tts import normalised

    hiss = np.array([0.001, -0.0005], dtype="float32")
    assert float(np.max(np.abs(normalised(np, hiss)))) <= 0.001 * 4 + 1e-6


def test_silence_is_left_alone():
    import numpy as np

    from jarvis.tts import normalised

    nothing = np.zeros(8, dtype="float32")
    assert float(np.max(np.abs(normalised(np, nothing)))) == 0.0
