from __future__ import annotations

import json
import threading
import time

from jarvis.transcript import Transcript


def test_ids_are_monotonic_from_one():
    transcript = Transcript()
    assert [transcript.add(f"line {i}").id for i in range(3)] == [1, 2, 3]
    assert transcript.cursor == 3


def test_since_returns_only_what_is_new():
    transcript = Transcript()
    transcript.add("first")
    transcript.add("second")
    assert [item.text for item in transcript.since(0)] == ["first", "second"]
    assert [item.text for item in transcript.since(1)] == ["second"]
    assert transcript.since(2) == []


def test_wait_for_returns_immediately_when_something_is_already_there():
    transcript = Transcript()
    transcript.add("already said")
    started = time.monotonic()
    assert [i.text for i in transcript.wait_for(0, timeout=5)] == ["already said"]
    assert time.monotonic() - started < 0.5


def test_wait_for_wakes_the_instant_something_arrives():
    """This is the whole point - an agent blocks here instead of polling."""
    transcript = Transcript()
    threading.Timer(0.2, lambda: transcript.add("spoken later")).start()

    started = time.monotonic()
    items = transcript.wait_for(transcript.cursor, timeout=10)
    elapsed = time.monotonic() - started

    assert [item.text for item in items] == ["spoken later"]
    assert 0.15 < elapsed < 2.0, "should wake on notify, not on timeout"


def test_wait_for_returns_empty_on_timeout_rather_than_raising():
    assert Transcript().wait_for(0, timeout=0.2) == []


def test_unaddressed_speech_is_recorded_but_does_not_satisfy_a_waiter():
    transcript = Transcript()
    transcript.add("chatting to someone else", addressed=False)
    assert len(transcript.since(0)) == 1, "recorded either way"
    assert transcript.wait_for(0, timeout=0.2, addressed_only=True) == []


def test_an_addressed_utterance_returns_the_chatter_with_it():
    transcript = Transcript()
    transcript.add("the one in the parser", addressed=False)
    transcript.add("jarvis fix that bug", addressed=True, command="fix that bug")

    items = transcript.wait_for(0, timeout=2, addressed_only=True)
    assert [i.text for i in items] == ["the one in the parser", "jarvis fix that bug"]
    assert [i.addressed for i in items] == [False, True]


def test_command_defaults_to_the_raw_text():
    assert Transcript().add("no wake word here").command == "no wake word here"


def test_command_holds_the_stripped_instruction():
    utterance = Transcript().add("jarvis open it", addressed=True, command="open it")
    assert (utterance.text, utterance.command) == ("jarvis open it", "open it")


def test_the_addressed_flag_survives_a_round_trip_to_disk(tmp_path):
    path = tmp_path / "heard.jsonl"
    transcript = Transcript(path)
    transcript.add("overheard", addressed=False)
    transcript.add("jarvis do it", addressed=True, command="do it")

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["addressed"] for line in lines] == [False, True]
    assert [line["command"] for line in lines] == ["overheard", "do it"]


def test_utterances_are_appended_to_the_file_as_jsonl(tmp_path):
    path = tmp_path / "heard.jsonl"
    transcript = Transcript(path)
    transcript.add("first")
    transcript.add("second")

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["text"] for line in lines] == ["first", "second"]
    assert [line["id"] for line in lines] == [1, 2]
    assert all(line["at"].endswith("+00:00") for line in lines)


def test_ids_continue_across_a_restart(tmp_path):
    path = tmp_path / "heard.jsonl"
    Transcript(path).add("before the restart")

    resumed = Transcript(path)
    assert resumed.cursor == 1
    assert resumed.add("after the restart").id == 2
    # A client holding cursor 1 must not be handed the old line again.
    assert [item.text for item in resumed.since(1)] == ["after the restart"]


def test_a_corrupt_transcript_line_does_not_stop_resume(tmp_path):
    path = tmp_path / "heard.jsonl"
    path.write_text('{"id": 4, "text": "fine"}\nnot json at all\n', encoding="utf-8")
    assert Transcript(path).add("next").id == 5


def test_old_utterances_are_dropped_but_ids_keep_climbing():
    transcript = Transcript(keep=3)
    for i in range(5):
        transcript.add(f"line {i}")
    assert transcript.cursor == 5
    assert [item.text for item in transcript.since(0)] == ["line 2", "line 3", "line 4"]
