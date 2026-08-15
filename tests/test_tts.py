from __future__ import annotations

import pytest

from jarvis.tts import NullSpeaker, SpeechEngine, _sapi_rate, iter_sentences


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


def test_speech_engine_survives_a_broken_backend():
    class Broken(NullSpeaker):
        def speak(self, text: str) -> None:
            raise RuntimeError("no audio device")

    engine = SpeechEngine(Broken)
    engine.say("This will fail.")
    assert engine.wait(timeout=5)
    engine.close()  # must not hang or raise
