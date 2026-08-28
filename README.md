# JARVIS

> **J**ust **A** **R**ather **V**ery **I**ntelligent **S**ystem

A voice assistant for a Windows desktop that runs entirely on the machine it is sitting on.
It listens, works out what you wanted, does it - clicking real buttons on real windows,
running real commands - and says what happened. No wake word, no cloud, no API key.

It used to have no model of its own: an agent connected over MCP and JARVIS was ears and a
mouth for it. That is still supported, and it is where all the interesting failures came
from, but as of 0.8.0 JARVIS owns the loop. **The reply is the speech.** There is no tool
for talking, so there is nothing to forget - what the model writes is what comes out of the
speakers. Four mechanisms trying to get that guarantee from outside are recorded in
[`DESIGN.md`](DESIGN.md) as failures.

**Windows only.** The ears would port - Whisper and PyAudio do not care - but the hands are
UI Automation and `SendInput`, and the voice is SAPI. Linux is not off the table, it just is
not started.

## Setup

```powershell
uv sync
```

That is it. Everything is on by default and no config file is needed.

**You need an OpenAI-compatible endpoint running before you start it.** This was built
against llama.cpp on loopback, and [`docs/example-configuration.md`](docs/example-configuration.md)
has the exact launcher, hardware and numbers. If nothing answers at `brain.url`, JARVIS says
so and stops - it does not come up half working. There is no switch to run it without a
model, because a JARVIS that listens, transcribes and answers nobody looks entirely well and
is not.

```powershell
.\jarvis.ps1 -Windowed   # start it in its own window and return
.\jarvis.ps1             # or run it here and watch the transcript
```

The first run downloads Whisper's `base.en`, about 150 MB, and caches it. Then just talk.
Everything heard goes through the loop, and JARVIS decides what was aimed at it - there is
no name to say.

The terminal it runs in shows the conversation as it happens: what it heard, every tool call
and the first line of what came back, and what it said.

```
you > what is the weather in melbourne
  > search_web(query='melbourne weather today')
    1. Melbourne, VIC - Weather  [bom.gov.au]
jarvis > Fifteen degrees and overcast, sir.
```

Under that sits one live line. It is the model thinking, streamed as it writes, replaced by
whatever it does next and gone when it answers - visible while it happens, kept by nothing,
which is all "collapsing" ever means. At the right hand end of it is how full the context is
and how much the last reply cost. Voice mode and chat mode draw through the same code, so
they look the same, and piped or redirected it degrades to plain lines with no escape codes.
Detail goes to `logs/jarvis.log`, which rotates, and every utterance to `logs/heard.jsonl`.
The reasoning is in that log too: it is the only record of why it did what it did, and a
voice session has nowhere else to keep one.

One throwaway request goes out at startup so the system prompt and the tool schemas are
already in the model server's cache. It costs a second or two of nobody's time and takes it
off the first answer, which is the one that would otherwise feel broken.

## Talking to it

Speech goes to Whisper on this machine, then to the model with the desktop tools attached,
then whatever the model writes is spoken. A phrase ends after 1.2s of non-speech
(`audio.pause_threshold`), which is the floor under how quickly anything can happen and is
set high deliberately - being cut off mid sentence is worse than waiting.

Two things follow from owning the loop that were impossible before.

**You can talk over it while it works.** The reply is read as it is generated and the room is
checked while that happens, so speaking abandons the half written answer where it stands and
the turn carries on knowing what you just said. Everything it had already found is kept, so
"no, the other one" builds on the look that has already happened rather than starting again.
The last call of a turn is the exception: it is one sentence from being spoken, and abandoning
it would lose the answer to work already done.

Cutting it off once it is *speaking* is a different thing and is off by default, because with
no echo cancellation an open microphone on speakers transcribes JARVIS itself. See
`audio.listen_while_speaking` - on headphones there is nothing to hear, so turn it on there.

**A turn that did work cannot end silent.** If the model runs out of tool calls without saying
anything, it is asked once more with the tools taken away, so prose is the only move left.
Staying quiet is still allowed and takes deliberate effort - a reply of a single hyphen, which
is what it should do when it hears somebody else's conversation.

You can also just type. In the window JARVIS is running in, start typing and the line appears
where the status usually sits; press enter and it goes in exactly where speech does - same
`you >`, same transcript, same everything after that. Nothing shows until you press a key, and
escape throws the line away. It is the answer to a room with somebody else in it, a word
Whisper will never get right however many times you say it, and anything you would rather not
say out loud. It works while the microphone is paused, too, since typing is plainly a choice
to say something.

Press Num Lock to stop listening from anywhere. The microphone stops being read, so nothing
is transcribed, logged or written to `heard.jsonl` until you press it again - not merely
withheld. Needs `uv sync --extra hotkey`; `service.hotkey = ""` disables it.

Asking works too. "Stop listening, I'm on a call" is a tool it has, and it tells you which
key brings it back, because from then on it cannot hear you ask. Either way the live line
says it is not listening, rather than claiming to be.

## Typing to it instead

```powershell
.\jarvis.ps1 chat
```

Same loop, same tools, same memories, no microphone - so it works over SSH, and it is much
the better place to work out why the model did something. Tool calls are printed as they
happen, you can scroll back, and no experiment costs a sentence out loud. `/tools`,
`/memories` and `/help`; Ctrl+D or `/quit` to leave. `--verbose` puts the tool results on
screen as well as in the log.

It needs no voice service running, which also means it cannot hear you and cannot speak. It
can still see and drive the desktop, but only the one it is actually logged into - UI
Automation does not reach the desktop from a remote session.

## Looking things up

`search_web` and `read_page`, through DuckDuckGo's HTML endpoint, which needs no key. The
snippets usually answer the question on their own; a page is only fetched when they do not,
and comes back as text with the markup stripped and cut to `brain.page_chars`.

**This is the one thing in the default install that leaves the machine**, so the startup line
says so every time and `brain.web = false` removes both tools. It is on by default because
there is no local version of it - off, the feature does not exist rather than falling back to
something.

Two things about that endpoint. Its limit is one query a second, which JARVIS keeps to rather
than discovers, because 300ms of waiting is invisible in a conversation and being refused
costs a whole turn; past it, the refusal arrives as a 202 carrying a challenge page, which is
a success code that most HTTP clients wave through. And it is somebody's HTML page rather than
somebody's contract, so when it changes shape a search returns "no results" rather than
nonsense. `brain.search_url` points at your own SearXNG instead, which has neither problem.

## What it remembers

Most of what makes the desk workable is only discoverable by getting it wrong: a window
whose accessibility tree is empty until it has been focused once, an application that takes
a moment to build itself, which of four identically labelled buttons is the one that works.
None of that is in a model's weights and none of it is worth writing into a prompt by hand,
because the next machine's list is different.

So JARVIS keeps its own, two ways. It calls `remember()` mid-turn when it works something
out, and the refusal it just hit says as much. And after any turn that used its hands, while
the answer is being read out, it looks back over what it did and writes down anything that
would have saved it a step. That second one costs nothing you can feel: the speech is playing
on another thread, so the model call happens in time nobody is waiting through.

Either way the whole list goes into its prompt at the start of every turn, so a lesson learned
at half past two is in play at half past three.

```
context/
  soul/jarvis.md                        who it is - character, nothing else
  tools/tools.md                        what it can do, generated from tools.py
  memories/navigation/os-navigation.md  how Windows behaves, by hand, shipped
  memories/navigation/user-navigation.md  what it works out about getting around
  memories/memories.md                  everything else it works out
```

Split by what each one changes for. `soul` is character: how to speak, when to stay quiet,
that its words end its turn. It is the same whoever is driving, so there is one file rather
than one per path, and it should almost never need editing. `tools` is generated. Everything
about the desk lives under `memories`, and every markdown file in there is read into the
prompt at any depth - so adding your own is dropping a file in.

The two halves of `navigation` are the same distinction one level down. `os-navigation.md`
ships and is edited by hand: how Windows behaves, true on any machine. `user-navigation.md`
is JARVIS's own and is not in git, because it is about this desk. When something in the
second turns out to be true of the first, move it over - that is a text edit, and it is the
whole reason they are separate files.

The memories are plain markdown bullets, so one that has gone wrong is fixed by opening the
file and deleting the line, and you can add your own. Capped by `brain.max_memory_chars`,
oldest dropped first - the right end to lose, since the desk changes and a lesson about an
application that has since been updated is worse than no lesson.

`soul/jarvis.md` is the prompt itself rather than a copy of it - there is nothing in the code
to drift from, and if it is missing the brain says so and does not start. Keep it short. The
more instructions it carries the more carelessly they are followed, which is the accessibility
tree lesson in different clothes.

Only `memories.md` is written by JARVIS, and only it is capped: the reference beside it is
bounded by whoever wrote it, and trimming the curated half to make room for the accumulated
half would be the wrong way round. The tool descriptions are not remembered at all: the
schemas go into every single request, so there is nothing to load and nothing to forget. They
are still the largest influence on its behaviour after the system prompt, and reading them
should not mean reading Python - so `jarvis tools` prints them and `--write` regenerates
`context/tools/tools.md`, with a test to catch drift. Change the wording in `tools.py` rather
than in the file: prose that drifts from a signature gets believed over it.

## The rest of the commands

```powershell
.\jarvis.ps1 status                # exit 0 if it is up, 2 if not
.\jarvis.ps1 next                  # block until you speak, then print it
.\jarvis.ps1 say "Opening it now"  # speak, muting the mic so it is not heard back
.\jarvis.ps1 look                  # number what is clickable in the window in front
.\jarvis.ps1 click 12 --expecting Reply
.\jarvis.ps1 screenshot            # a picture of the window in front
.\jarvis.ps1 chat                  # type to it instead of speaking, no microphone
.\jarvis.ps1 tools                 # what the brain can do, as the model is told it
.\jarvis.ps1 config                # every setting in effect, and where it came from
.\jarvis.ps1 mcp                   # MCP server over stdio, for a coding agent
.\jarvis.ps1 --list-devices        # find your microphone
```

Call the script by its full path and it works from any directory. The service has to be
running for the rest to do anything - it is the process that owns the audio hardware, which
is why `say` from another terminal can mute the same microphone that is listening. It runs
as `uv`/`python`, so `Get-Process jarvis` finds nothing; use `jarvis.ps1 status`.

## Screen control

The problem this solves: handing a model the whole accessibility tree does not work. A Teams
window is 810 nodes, almost all of them panes, groups and static text, and a 35B model given
the lot picks something plausible and wrong. That same window has 54 elements you can
actually act on. Outlook in a browser: 833 nodes, 177 targets.

So the model never sees the tree, and never sees a coordinate either. It gets a numbered
list and names a number:

```powershell
.\jarvis.ps1 look "outlook" --matching reply
#    1  Button      Reply
#    2  Button      Reply all
.\jarvis.ps1 click 1 --expecting Reply
```

`--expecting` is the interesting part. It is required and it is checked against the label
before anything is pressed. A number left over from a scan taken before the list scrolled
still resolves to a perfectly good coordinate, and that is how automation ends up pressing
delete on the wrong row. Naming what you expect turns a misclick into a refusal.

Three more things are checked. A scan expires after 60s. Whatever is under the point has to
still be the target, which catches occlusion for free - a desktop icon behind a terminal is
genuinely not clickable, and the refusal says so instead of clicking the terminal. And a
minimised window is refused outright, because it still reports plausible coordinates left
over from wherever it was last drawn.

Some of it needs no scan at all. `press_keys` knows the media keys, and Windows routes those
to whatever is playing, so "pause my music" is one call with no window to find. The
taskbar scans as a window called `Taskbar`, one target per pinned app, which is the sane way
to launch something. And typing with no target goes wherever the caret already is, which is
the only way into the Start menu - it reports a single element covering itself, so a scan
comes back flagged as having nothing clickable rather than offering a target that cannot
work.

Two limits worth knowing. Windows silently refuses input from an unelevated process to any
window running as administrator: the tree reads perfectly and every click does nothing. And
UI Automation only reaches the desktop from the signed-in session, the same way an audio
device does, so a service in session 0 sees nothing.

`screen.control = false` leaves the read-only half - looking and screenshots, no pointer.
`jarvis look --marks` writes `logs/marks.png` with a numbered box burned over every target,
which turns "why did it click the wrong thing" from unanswerable into obvious.

No new dependencies for any of it: UI Automation comes through `comtypes`, which was already
here for SAPI, and input goes through `SendInput` in `ctypes`.

## The MCP server, which is still here

Everything before 0.8.0 was built for this: an agent connected over MCP, and JARVIS was ears
and a mouth for it. `jarvis mcp` still runs and the tools still work, and the reasoning behind
them is the most interesting thing in [`DESIGN.md`](DESIGN.md). But it is no longer a mode of
operation - the brain always runs, so with a client connected as well, both would answer.
Read this as the record of how the loop came to be owned rather than as a way to run it.

Hand the agent [`context/soul/jarvis.md`](context/soul/jarvis.md) as context. Most clients
want that as a copy in a rules directory, and a copy is a thing that goes stale - so point
`service.agent_rules` at it:

```powershell
.\jarvis.ps1 rules              # does the guide the agent reads match this one?
.\jarvis.ps1 rules --install    # make it
```

Worth its own command because the failure is invisible and total. A guide written before the
tools were renamed names tools that no longer exist, the model reads it every turn and
believes it over the schemas, and nothing in the session says so.

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "uv",
      "args": ["run", "--no-sync", "--directory", "/absolute/path/to/jarvis", "jarvis", "mcp"]
    }
  }
}
```

`--no-sync` matters. Without it `uv run` reinstalls the project whenever its metadata
changes, which means replacing `.venv\Scripts\jarvis.exe` - and that exe is the running MCP
server, which Windows will not let anything overwrite. Bumping the version then makes every
start fail with "The process cannot access the file".

Twelve tools appear. `converse(say, then)` is the whole conversation: it speaks and then
blocks for the reply, so answering and listening cannot come apart. That design exists
because two tools came apart repeatedly - and it still was not enough, which is the whole
argument for 0.8.0. **A required argument constrains a call that happens, it cannot cause a
call to happen.** Nothing in MCP can: elicitation routes to the human, and sampling is
deprecated by SEP-2577 and no client tried here has ever offered it.

So `overhear.py` stops asking and reads instead. A client that writes its conversation to
disk as it goes leaves the reply it typed rather than spoke sitting in a file, and JARVIS
watches for it and says it. Jank, and it works - every historical failure in this repo was
checked against it and it recovers all of them. Point `service.agent_sessions` at the
directory of sessions; empty, which is the default, switches it off. Nothing is guessed at,
because one client's layout is not a standard: a file that is not a list of messages is left
alone and logged once, since reading somebody's half-understood format out loud is worse than
staying quiet. The messages themselves are expected in the ordinary Anthropic API shape.

## What leaves this machine

Nothing, unless you ask for it. At startup JARVIS prints exactly what each stage is doing:

```
ears: whisper (local) -> brain: 127.0.0.1:8081 (local) -> voice: auto (local). Nothing leaves this machine.
```

Three settings can change that, all of them opt in and all of them named in that line:

| Setting | Sends | To |
| --- | --- | --- |
| `stt.backend = "google"` | your raw microphone audio | Google |
| `tts.engine = "edge"` | every reply, as text | Microsoft |
| `brain.url` off loopback | every word of every conversation | wherever it points |

One thing is not JARVIS's to promise: `screenshot` hands a picture of your screen to
whatever model is connected, and so does every scan while `screen.send_image` is on. A
picture of your screen has whatever was on it at the time. Local model, local picture; on a
model with no vision it is also a megabyte of payload for nothing, which is the other reason
to turn it off.

## Configuration

Three ways, each beating the last: `config/jarvis.json`, then `JARVIS_*` environment
variables, then command line flags.

```powershell
jarvis config              # everything in effect, and where it came from
jarvis config --defaults   # just the built-in defaults
```

`config/defaults.json` lists every option with its default. It is generated from the code
and a test fails if the two drift, because a hand-written example goes stale the first time
someone changes a default. `config/jarvis.toml.example` is the annotated version of the
same thing, kept because comments explain a trade-off better than a schema can.

JSON has no comments, so any key beginning with `_` is ignored and can hold the reason a
setting is what it is. Copy only the bits you want; anything absent keeps its default.

## Architecture

```
 mic thread ──▶ queue ──▶ STT ──▶ transcript ──┬──▶ brain ──▶ tools ──▶ speech
      ▲                                        │       (in this process)
      └──── muted while speaking ◀─────────────┴──▶ GET /heard (blocks) ──▶ MCP agent
```

| Module | Role |
| --- | --- |
| `cli.py` | Argument parsing, wiring, and every command |
| `service.py` | Owns the hardware, serves loopback HTTP |
| `brain.py` | The agent loop, the model client, and the system prompt |
| `tools.py` | What the brain can do, as schemas and dispatch |
| `chat.py` | The same loop with a keyboard instead of a microphone |
| `ui.py` | The terminal: scrolling conversation, one live line, no dependency |
| `typed.py` | A line typed into the voice session, taken as though it were heard |
| `memories.py` | The list JARVIS writes for itself and reads back every turn |
| `transcript.py` | Append-only record with blocking reads |
| `client.py` | Client for the service, shared by the CLI and MCP |
| `mcp_server.py` | The tools a connected coding agent can call instead |
| `microphone.py` | Background capture, phrase splitting, mute |
| `vad.py` | Whether a buffer is speech: Silero, or loudness as a fallback |
| `stt.py` | Local Whisper transcription, with Google as an opt in |
| `tts.py` | Speech worker thread, SAPI and Edge backends, sentence splitting |
| `hotkey.py` | The global key that stops and starts listening |
| `overhear.py` | Reading a coding agent's prose off disk and speaking what it never said |
| `screen.py` | Cutting the accessibility tree to numbered targets, and refusing stale ones |
| `uia.py` | UI Automation through comtypes: the only Windows-specific module |
| `hands.py` | Synthetic clicks and keystrokes, through SendInput |
| `marks.py` | The numbered boxes drawn onto a screenshot |
| `reap.py` | Clearing MCP servers that outlived their client |
| `echo.py` | Recognising JARVIS's own voice coming back |
| `config.py` | Defaults, TOML, environment |

[`DESIGN.md`](DESIGN.md) has the reasoning behind everything that is not obvious, including
all the things that were tried and removed.

## Not done yet

- **A better way to forget.** There is no compaction. `brain.history_turns` keeps the last
  six turns whole and deletes the rest - chosen because voice conversations are short, and
  cutting at a turn boundary is what keeps a tool result from outliving the call it answered.
  It is still just dropping things: ask about something from ten turns ago and it is gone
  without a trace, and nothing summarises what went. Every real option costs something a
  voice loop can feel - summarising means a model call between turns, and a rolling summary
  means the summary is in every request forever.
- **Speech interruption on speakers.** Cutting JARVIS off mid sentence needs the microphone
  open while it talks, and with no acoustic echo cancellation that means it transcribes
  itself - which it did, answering its own weather forecast with "That's right. Is there
  anything else I can help you with?" The text comparison in `echo.py` is the only defence
  and a long reply beat it once already, so `audio.listen_while_speaking` is off and
  headphones are the workaround. Doing it properly means real AEC on the capture path.

## Development

```powershell
uv run pytest        # 592 tests, no hardware, model or network needed
uv run ruff check .
uv run ruff format .
```

## License

MIT
