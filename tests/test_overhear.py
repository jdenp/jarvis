"""Reading the agent's own prose off disk and speaking what it never said.

The last resort, and the only thing that worked. Four attempts to make an agent
remember to speak are recorded in DESIGN.md; every one of them put words into a
tool result, and a tool result is advice. This asks the agent for nothing.

The message shape here is taken from a real Cline transcript, including the two
lines from one session that were written and thrown away.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis.overhear import (
    Overheard,
    assistant_prose,
    newest_transcript,
    transcripts,
    worth_speaking,
)

# The lines the user should have heard, and did not.
LEAD_IN = "Spotify is open. Let me press play to start music."
ANSWER = "Done - Spotify's open and Katy Perry's \"Harleys In Hawaii\" is playing now."


def session(tmp_path, name, messages, *, age=0.0, envelope=True):
    """One session directory, shaped as Cline writes them.

    The envelope is what marks a transcript as Cline's, and what this refuses to
    read without - `origin` with a source, and a session id.
    """
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.messages.json"
    blob = {"version": 1, "messages": messages}
    if envelope:
        blob["sessionId"] = name
        blob["agent"] = "lead"
        blob["origin"] = {"source": "cli", "mode": "user", "sessionId": name, "version": "3.0.58"}
    path.write_text(json.dumps(blob), encoding="utf-8")
    if age:
        stamp = time.time() - age
        import os

        os.utime(path, (stamp, stamp))
    return path


def assistant(*parts):
    return {"id": "x", "role": "assistant", "ts": 0, "content": list(parts)}


def text(body):
    return {"type": "text", "text": body}


def thinking(body):
    return {"type": "thinking", "thinking": body}


def tool_use(name, **kwargs):
    return {"type": "tool_use", "name": name, "input": kwargs}


# ------------------------------------------------------------------- parsing


def test_the_prose_is_pulled_out_and_nothing_else():
    """Thinking is verbose, internal, and frequently about the user rather than
    to them. Tool calls are not speech."""
    blob = {
        "messages": [
            {"role": "user", "content": "open Spotify and play"},
            assistant(
                thinking("The user said Jarvis, can you open Spotify..."),
                tool_use("jarvis__look_at_screen", window="Taskbar"),
            ),
            assistant(thinking("Spotify is at target 8"), text(LEAD_IN)),
            assistant(tool_use("jarvis__press_keys", keys="playpause"), text(ANSWER)),
        ]
    }
    assert assistant_prose(blob) == [LEAD_IN, ANSWER]


def test_a_plain_string_message_still_counts():
    blob = {"messages": [{"role": "assistant", "content": "  Right you are, sir.  "}]}
    assert assistant_prose(blob) == ["Right you are, sir."]


def test_the_user_is_never_quoted_back():
    blob = {"messages": [{"role": "user", "content": [text("open Spotify")]}]}
    assert assistant_prose(blob) == []


def test_a_transcript_with_no_messages_is_not_an_error():
    assert assistant_prose({}) == []
    assert assistant_prose({"messages": None}) == []


# ----------------------------------------------------------- worth saying


@pytest.mark.parametrize("line", [LEAD_IN, ANSWER, "Half past two, sir."])
def test_a_spoken_reply_is_worth_speaking(line):
    assert worth_speaking(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "Here it is:\n```python\nprint(1)\n```",
        "| col |\n|---|\n| x |",
        "Changes:\n- one\n- two",
        "Steps:\n1. first\n2. second",
        "## Settings\nwhisper_model: small.en",
    ],
)
def test_prose_written_for_the_eye_is_left_alone(line):
    """It was written for a reader. Reading markdown out loud is worse than
    saying nothing - asterisks come out as the word asterisk."""
    assert worth_speaking(line) is False


def test_something_far_too_long_is_left_alone():
    assert worth_speaking("word " * 200) is False
    assert worth_speaking("word " * 200, limit=100_000) is True, "the limit is the only reason"


# ------------------------------------------------------- picking a session


def test_the_newest_transcript_wins(tmp_path):
    """The MCP server is told nothing about which session spawned it, so the one
    written to most recently is the only available guess."""
    session(tmp_path, "old_one", [assistant(text("stale"))], age=600)
    session(tmp_path, "new_one", [assistant(text("fresh"))])
    assert newest_transcript(tmp_path).parent.name == "new_one"


def test_a_directory_without_a_transcript_is_skipped(tmp_path):
    (tmp_path / "empty_session").mkdir()
    session(tmp_path, "real_one", [assistant(text("hello"))])
    assert [p.parent.name for p in transcripts(tmp_path)] == ["real_one"]


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert transcripts(tmp_path / "not-there") == []
    assert newest_transcript(tmp_path / "not-there") is None


# --------------------------------------------------------- reading forward


def test_nothing_is_said_about_a_conversation_already_finished(tmp_path):
    """Whatever is in the file when a reply falls due belongs to earlier turns.
    Saying it now would be answering a finished conversation."""
    session(tmp_path, "s1", [assistant(text("from an earlier turn"))])
    watcher = Overheard(tmp_path)
    watcher.catch_up()
    assert watcher.anything_new() == []


def test_prose_written_after_that_is_picked_up(tmp_path):
    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [assistant(text("earlier"))])
    watcher.catch_up()

    session(tmp_path, "s1", [assistant(text("earlier")), assistant(text(LEAD_IN))])
    assert watcher.anything_new() == [LEAD_IN]


def test_the_same_line_is_not_said_twice(tmp_path):
    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [])
    watcher.catch_up()
    session(tmp_path, "s1", [assistant(text(ANSWER))])
    assert watcher.anything_new() == [ANSWER]
    assert watcher.anything_new() == [], "the file has not moved on"


def test_both_lines_of_one_turn_come_back(tmp_path):
    """The session this is built from wrote a lead-in and then an answer. Both
    were worth hearing."""
    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [])
    watcher.catch_up()
    session(tmp_path, "s1", [assistant(text(LEAD_IN)), assistant(text(ANSWER))])
    assert watcher.anything_new() == [LEAD_IN, ANSWER]


def test_a_half_written_file_is_waited_out(tmp_path):
    """The client rewrites the whole file, so a read can land mid-write. There is
    nothing to do but come back on the next poll."""
    watcher = Overheard(tmp_path)
    path = session(tmp_path, "s1", [assistant(text("fine"))])
    watcher.catch_up()

    path.write_text('{"messages": [{"role": "assist', encoding="utf-8")
    assert watcher.anything_new() == []

    session(tmp_path, "s1", [assistant(text("fine")), assistant(text(ANSWER))])
    assert watcher.anything_new() == [ANSWER]


def test_unspeakable_prose_is_read_past_rather_than_read_out(tmp_path):
    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [])
    watcher.catch_up()
    session(
        tmp_path,
        "s1",
        [assistant(text("Here:\n```\ncode\n```")), assistant(text(ANSWER))],
    )
    assert watcher.anything_new() == [ANSWER], "the code block was skipped, not the answer"


# --------------------------------------------------------------- tidying up


def test_emphasis_is_stripped_rather_than_the_sentence_rejected():
    """SAPI reads `**947**` as "asterisk asterisk nine four seven". The sentence
    underneath is still a good answer."""
    from jarvis.overhear import for_speaking

    assert for_speaking("About **947 output tokens** across **4 runs**.") == (
        "About 947 output tokens across 4 runs."
    )


def test_emoji_go_too():
    from jarvis.overhear import for_speaking

    assert for_speaking("test received \U0001f44d") == "test received"
    assert for_speaking("hello \U0001f44b") == "hello"


def test_a_spoken_line_is_one_line():
    from jarvis.overhear import for_speaking

    assert for_speaking("Done, sir.\n\nAnything else?") == "Done, sir. Anything else?"


def test_tidying_happens_on_the_way_out(tmp_path):
    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [])
    watcher.catch_up()
    session(tmp_path, "s1", [assistant(text("That is **done**, sir \U0001f44d"))])
    assert watcher.anything_new() == ["That is done, sir"]


def test_something_that_tidies_down_to_nothing_is_not_spoken(tmp_path):
    """A message that was only an emoji leaves an empty string behind, and
    speaking an empty string is a pause for no reason."""
    from jarvis.overhear import for_speaking

    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [])
    watcher.catch_up()
    session(tmp_path, "s1", [assistant(text("👍")), assistant(text(ANSWER))])
    assert watcher.anything_new() == [for_speaking(ANSWER)]


# --------------------------------------------------- Cline's format, and only


def test_clines_envelope_is_recognised():
    from jarvis.overhear import looks_like_cline

    assert looks_like_cline(
        {
            "messages": [],
            "sessionId": "1787820869065_pvlov",
            "agent": "lead",
            "origin": {"source": "cli", "version": "3.0.58"},
        }
    )


@pytest.mark.parametrize(
    "blob",
    [
        {},
        {"messages": []},
        {"messages": [], "sessionId": "x"},
        {"messages": [], "origin": {"source": "cli"}},
        {"messages": "not a list", "sessionId": "x", "origin": {"source": "cli"}},
        {"messages": [], "sessionId": "x", "origin": "not a dict"},
    ],
)
def test_anything_else_is_not_read(blob):
    """Reading a format nobody has verified out loud is a worse failure than
    staying quiet, so an unfamiliar file is left alone rather than guessed at."""
    from jarvis.overhear import looks_like_cline

    assert looks_like_cline(blob) is False


def test_a_transcript_without_the_envelope_is_left_alone(tmp_path, caplog):
    import logging

    watcher = Overheard(tmp_path)
    session(tmp_path, "s1", [], envelope=False)
    watcher.catch_up()
    session(tmp_path, "s1", [assistant(text(ANSWER))], envelope=False)

    with caplog.at_level(logging.WARNING, logger="jarvis.overhear"):
        assert watcher.anything_new() == []
    assert "not in the format this understands" in caplog.text
    assert "overhear.py" in caplog.text, "and where to go to teach it another one"


def test_it_only_complains_once(tmp_path, caplog):
    import logging

    watcher = Overheard(tmp_path)
    with caplog.at_level(logging.WARNING, logger="jarvis.overhear"):
        for index in range(3):
            session(tmp_path, "s1", [assistant(text(f"line {index}"))], envelope=False)
            watcher.anything_new()
    assert caplog.text.count("not in the format") == 1
