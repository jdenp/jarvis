from __future__ import annotations

import json
import threading
import time

from jarvis.transcript import ToolCall, Transcript


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


def test_a_record_of_tool_calls_keeps_what_came_back():
    """Same ids, same cursor, same waiting - one more field, because the call
    and what it gave back are drawn differently at both ends."""
    calls = Transcript(item=ToolCall)
    calls.add("look_at_screen(window='teams')", gave="Teams - 14 targets")

    [call] = calls.since(0)
    assert call.id == 1
    assert call.as_dict() == {
        "id": 1,
        "text": "look_at_screen(window='teams')",
        "gave": "Teams - 14 targets",
        "at": call.at,
    }


def test_a_call_with_nothing_to_say_for_itself_is_still_a_call():
    calls = Transcript(item=ToolCall)
    assert calls.add("press_keys(keys='win')").gave == ""


def test_pause_stops_recording():
    transcript = Transcript()
    transcript.add("before")
    transcript.pause()
    transcript.add("during pause")
    assert transcript.paused is True
    assert [item.text for item in transcript.since(0)] == ["before"]
    # cursor still advances
    assert transcript.cursor == 2


def test_resume_resumes_recording():
    transcript = Transcript()
    transcript.add("before")
    transcript.pause()
    transcript.add("during pause")
    transcript.resume()
    assert transcript.paused is False
    transcript.add("after resume")
    assert [item.text for item in transcript.since(0)] == ["before", "after resume"]


def test_pause_resume_does_not_notify_waiters():
    """A paused utterance must not wake a waiter."""
    transcript = Transcript()
    transcript.pause()
    threading.Timer(0.2, lambda: transcript.add("during pause")).start()
    items = transcript.wait_for(0, timeout=1)
    assert items == []


def test_toggle_pause_twice_is_idempotent():
    transcript = Transcript()
    assert transcript.pause() is True
    assert transcript.pause() is False  # already paused
    assert transcript.resume() is True
    assert transcript.resume() is False  # already resumed
