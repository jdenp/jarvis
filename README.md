# JARVIS

> **J**ust **A** **R**ather **V**ery **I**ntelligent **S**ystem

A local voice service. It listens on your microphone and hands everything it hears to
whatever agent is connected. That agent decides what was meant for it, and speaks back
through your speakers. Switch screen control on and it can also see the desktop and
click on it.

JARVIS has no model of its own - it is ears, a mouth and a pair of hands, and the agent
is the brain.

## Features

- **Fully local.** Whisper transcribes on this machine and the voice is the offline Windows
  one. Nothing leaves the machine, and JARVIS prints a line at startup saying so.
- Runs on CPU with no setup, or on the GPU for much quicker transcription. See
  [Speech recognition](#speech-recognition).
- **Blocking reads, not polling.** Listening returns the instant you finish a sentence,
  so an agent waits on it rather than asking repeatedly.
- MCP server, so Cline and friends see the microphone as tools they can call
- A plain CLI for everything else
- **Speech detection, not loudness.** A footstep is as loud as a word, and under a
  loudness test it holds a phrase open until the time limit. Silero scores each 32ms frame
  instead: measured here, thumps as loud as speech score 0.006, and the same sentence 24dB
  quieter scores the same as the original - so it also hears you without your raising your
  voice. It costs 0.19% of one core and no VRAM
- **No wake word at all.** Everything heard is passed on verbatim and the agent judges
  what was addressed to it - no name to say, and no string matching to produce phantom
  detections
- `check_for_speech` for steering mid task, since nothing can preempt an agent
- Half duplex with an echo guard, so JARVIS never transcribes its own voice
- Append-only transcript with monotonic ids, so nothing is missed across a reconnect
- **Screen control by number, not by pixel.** An agent asks what is on screen and gets
  back a short numbered list of what can be clicked - one Teams window is 810
  accessibility nodes and 54 of them are things you can act on. It names a number and a
  label it expects to find there; if the label no longer matches, nothing is pressed. Off
  by default, see [Screen control](#screen-control)
- **Hotkey shortcut.** Press Num Lock to stop listening without touching the agent. The
  microphone stops being read, so nothing is transcribed, logged or written to
  `heard.jsonl` until you press it again - not merely withheld from the agent. Install
  with `uv sync --extra hotkey` first. Change the key with `service.hotkey`, or set it to
  `""` to disable. The hook is global and does not swallow the keypress, so End still
  works normally in other apps - so Num Lock still flips the numeric keypad, and it
  toggles JARVIS wherever you press it.

## Requirements

- Python 3.12+
- A microphone

## Setup

```powershell
uv sync
```

That is the whole setup. **Every feature is on by default and no config file is needed.**
The defaults live in the dataclasses in `config.py`, `Config.load()` returns them when it
finds no file, and `config/defaults.json` is generated from them so you can read exactly
what you are getting. Out of the box:

| | |
| --- | --- |
| voice | `converse()`, and speech handed back as an error if a turn would end unanswered |
| screen control | clicking, typing, scrolling and keys, not only looking |
| the marked screenshot | drawn every scan, and sent to the agent as an image |
| full duplex | the microphone stays open while JARVIS talks, so you can cut it off |
| Num Lock | stops and starts listening from anywhere |

Two of those cost something and are the first to turn off if they bite. The marked
screenshot adds a full screen grab, about half a second, to every scan, and sending it is
payload for nothing on a model that cannot read images. Full duplex has no acoustic echo
cancellation behind it, so on speakers JARVIS can hear itself - headphones make it free,
and `audio.listen_while_speaking = false` makes it go away.

```powershell
copy config\jarvis.toml.example config\jarvis.toml   # only if you want to change something
```

The first run downloads the Whisper model (`base.en`, about 150 MB) and caches it. After
that, transcription is offline.

## Usage

```powershell
.\jarvis.ps1 -Windowed             # start it in its own terminal window, return immediately
.\jarvis.ps1                       # or run it in this terminal
.\jarvis.ps1 status                # exit 0 if it is up, exit 2 if not
.\jarvis.ps1 next                  # blocks until you speak, no timeout
.\jarvis.ps1 say "Opening it now"  # speaks it, muting the mic so it is not heard back
.\jarvis.ps1 look                  # number what is clickable in the window in front
.\jarvis.ps1 click 12 --expecting Reply   # press one of those numbers
.\jarvis.ps1 screenshot            # a picture of the window in front
.\jarvis.ps1 rules                 # is the agent reading the current guide?
.\jarvis.ps1 mcp                   # MCP server over stdio, for Cline and friends
.\jarvis.ps1 --list-devices        # find your microphone
.\jarvis.ps1 --device 1            # use a specific one
```

Call the script by its full path and it works from any directory without changing yours,
which is what an agent should do. There is no `jarvis` on PATH unless you activate the venv.

`-Windowed` is what an agent should use. It leaves the live transcript on screen instead of
burying the service in a background process, and it refuses to start a second copy rather
than failing to bind the port.

The service must be running for the others to do anything - it is the process that owns the
audio hardware, which is why `say` from a separate terminal can still mute the same
microphone that is listening. It runs as `uv`/`python`, so `Get-Process jarvis` finds
nothing; use `jarvis.ps1 status`.

Just talk. Everything heard goes to the agent verbatim and it decides what was aimed at
it. Logs rotate in `logs/jarvis.log`; everything heard is appended to `logs/heard.jsonl`.

## Connecting an agent

Hand the agent [`jarvis.md`](jarvis.md) as context - it explains the tools, how to speak
well, and the limits worth knowing. For Cline that means
`Documents\Cline\Rules\jarvis.md`, and **a copy there is a thing that goes stale**:

```powershell
.\jarvis.ps1 rules              # does the guide the agent reads match this one?
.\jarvis.ps1 rules --install    # make it
```

Worth its own command because the failure is invisible and total. A guide written before
the tools were renamed names tools that no longer exist, the model reads it every turn and
believes it over the schemas, and nothing in the session says so - it just quietly goes
back to the old loop. Measured here: a guide three days out of date was still telling the
model to call `wait_for_speech()` and to answer with a bare `say(answer)`, so it never
learned that `then` existed and dropped the second call. Hardlinking the file to this repo
also works, until a `git checkout` replaces it and silently breaks the link.

For Cline, add to your MCP settings:

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

`--no-sync` matters on Windows. Without it `uv run` reinstalls the project whenever its
metadata changes, which means replacing `.venv\Scripts\jarvis.exe` - and that exe is the
running MCP server, which Windows will not let anything overwrite. Bumping the version
then makes every start fail with "The process cannot access the file". The project is
installed editable, so code changes need no sync; run `uv sync` by hand when dependencies
change.

I recommend Cline for this - it handles the voice loop well, respects the blocking read
pattern, and doesn't poll. The MCP tools map cleanly to the JARVIS model of ears + mouth
with the agent as brain. If you are using something else, the loopback HTTP API at
`http://127.0.0.1:8770` covers everything the MCP tools do.

Seven tools appear: `converse(say, then)`, `check_for_speech()`,
`pause_transcription()`, `resume_transcription()`, `voice_status()`, `look_at_screen(...)`
and `screenshot(...)`. Turning on screen control adds five more - see
[Screen control](#screen-control).

`converse` is the whole conversation. It speaks `say` aloud and then blocks for the reply,
returning it in the same result, so answering and listening cannot come apart - there is
no second call to forget and no other tool to pick. `say=""` listens without speaking, and
is refused while a reply is owed, because with nothing through the speakers that is a claim
to have answered. `then="keep_working"` speaks and returns at once for a holding line
before slow work. Both arguments are required; the schema rejects the call before the tool
body runs.

Two tools became one after two live sessions where the agent listened, wrote its reply
into its own chat text and ended the turn without speaking. The limit is worth knowing
before you rely on it: **a required argument constrains a call that happens, it cannot
cause a call to happen.** Nothing in MCP can - elicitation and `InputRequiredResult` route
to the human, and Cline declares no `sampling` capability, so the server can never obtain
model output. Three softer attempts were built and removed as jank; what they were and why
they failed is in [`DESIGN.md`](DESIGN.md).

Anything else drives the same service through the CLI:

```powershell
$j = "$PWD\jarvis.ps1"   # or wherever you checked it out
while ($true) {
  $text = & $j next                  # blocks until spoken to
  if ($text) { & $j say (your-agent $text) }
}
```

Two things worth knowing before you build on it:

- **Nothing preempts an agent mid-turn.** If it is thirty seconds into a build, your speech
  waits until it next listens. Cooperative, not preemptive, and no transport
  changes that.
- **A quiet session returns empty results.** A listen blocks for
  `service.max_wait_seconds` (55s by default) and returns nothing if you have not spoken.
  Some clients count repeated identical results as a stuck loop and end the session, so if
  yours allows a long tool timeout, raise `max_wait_seconds` to match and it will return
  empty far less often.
- **The latency floor is `audio.pause_threshold`**, 1.2s by default: that much non-speech
  before JARVIS decides your sentence ended, plus about 0.2s of Whisper.
  Transport from transcription to the agent costs ~0.0s, so this is the knob that
  matters. It is set high deliberately - being cut off mid sentence is worse than
  waiting.
- **Background noise no longer holds a phrase open.** A pause is measured in frames that
  are not speech, so noise has to sound like a voice to count. `audio.pause_quiet_fraction`
  additionally lets a pause survive a brief interruption, and `audio.phrase_time_limit`
  (60s) is the last resort. A television with people talking on it is the case none of
  this solves - that needs speaker identification.

## Speech recognition

Whisper runs on this machine. On startup you may see:

```
Whisper is not usable on cuda (Library cublas64_12.dll is not found or cannot be loaded).
Whisper model base.en ready on cpu (int8).
```

That is the fallback working - it proves the device with a real inference before trusting
it, and drops to CPU if CUDA will not load. Nothing is broken, but **CPU transcription is
slow**, and gets sharply slower the longer you speak: a short sentence takes about a
second, twenty seconds of speech takes twelve.

To use the GPU, install the CUDA runtime as pip packages - no system CUDA install needed:

```powershell
uv sync --extra cuda
```

then in `config/jarvis.json`:

```json
{
  "stt": {
    "whisper_model": "small.en",
    "whisper_device": "auto",
    "whisper_compute_type": "int8_float16"
  }
}
```

`auto` falls back to CPU if CUDA still will not load. Budget about 340 MB of VRAM for
`base.en` and 560 MB for `small.en` - most of that is the CUDA context rather than the
model, so a smaller model saves less than you would think. `small.en` is the more accurate
of the two, particularly on names and accents.

## Screen control

On by default. Looking at the screen reads the accessibility tree and touches nothing;
clicking moves your real pointer and types on your real keyboard. If you want the
read-only half:

```json
{ "screen": { "control": false } }
```

That leaves `look_at_screen` and `screenshot`, and drops `focus_window`, `click`,
`type_text`, `scroll` and `press_keys`. Restart the MCP server after changing it either
way; the tools are registered at startup.

It was off by default to begin with, on the argument that moving someone's pointer should
be opted into. That was wrong in practice: an agent cannot discover the flag on its own,
and the failure when it is off is indistinguishable from the feature being broken - one
session spent four calls refusing to touch a minimised window while the tool that would
have restored it was not registered.

**The problem this solves.** Handing an agent the whole accessibility tree does not work.
A Teams window is 810 nodes, almost all of them panes, groups and static text, and a
model given the lot picks something plausible and wrong. Measured here, the same window
has 54 elements you can actually act on. Outlook in a browser: 833 nodes, 142 targets.

So the agent never sees the tree, and never sees a coordinate either. It gets a numbered
list, and names a number:

```powershell
.\jarvis.ps1 look "outlook" --matching reply
#    1  Button      Reply
#    2  Button      Reply all
.\jarvis.ps1 click 1 --expecting Reply
```

`--expecting` is the interesting part. It is required, and it is checked against the
label before anything is pressed. A number left over from a scan taken before the list
scrolled still resolves to a perfectly good coordinate, and that is how automation ends
up pressing delete on the wrong row. Naming what you expect turns a misclick into a
refusal.

Three more things are checked before a click lands:

- **The scan expires** after `screen.max_scan_age_seconds`, 60s by default.
- **What is under the point** has to still be the target. That catches occlusion for
  free: a desktop icon behind a terminal window is genuinely not clickable, and the
  refusal says so instead of clicking the terminal.
- **A minimised window is refused outright.** It still reports a full tree with plausible
  coordinates left over from wherever it was last drawn, so scanning one hands back
  numbers that point at other applications entirely.

**A very crowded window is sampled, not cut short.** Two hundred targets fit, which
covers anything normal. Past that the list is an even spread across the window and says
so. It used to be the first sixty in reading order, which was silently catastrophic: the
tail of that order is the bottom of the window, so on a media player it amputated the
transport bar. Asked to press play in Spotify, 166 targets became the top 60 and the play
button was not among them - the request was impossible rather than hard, and nothing said
why.

**Labels that repeat get placed.** A browser offers four buttons called Close with
nothing to choose between them, so the repeats come back as `Close, after "Gutenberg /
Alpha - GitLab"` - which is how anyone reads a tab strip. Only the repeats; on a unique
label it would be noise.

**And a picture, as the fallback.** `screenshot` returns the window in front as an
image, for when seeing it is the point rather than pressing it - an error dialog to read,
a chart, anything the accessibility tree does not describe. It needs a model that can
read images, and `look_at_screen` beats it for anything you intend to act on, being
smaller, exact, and clickable. `--numbers` gives you both at once.

**The marked screenshot is for you, not the model.** `jarvis look --marks` writes
`logs/marks.png` with a numbered box burned over every target, which turns "why did it
click the wrong thing" from unanswerable into obvious. Needs `uv sync --extra screen`
for Pillow. The agent's own scans do not draw one unless `screen.marks_file` is set,
because a full screen grab is about half a second. A vision model can read the same
image - `screen.send_image` - but with no `--mmproj` loaded it is a megabyte of cost for
something unreadable, so that is off too.

**Some of it needs no scan.** `press_keys` knows the media keys - `playpause`,
`nexttrack`, `volumeup`, `mute` and the rest - and Windows routes those to whatever is
playing, so "pause my music" is one call with no window to find. The shell's own windows
are scannable too, under `Taskbar`: 39 elements down to 25 targets, one per pinned app,
which is the sane way to launch something. And `type_text` with no target types wherever
the caret already is - the Start menu after `press_keys("win")` reports a single element
covering itself, so there is nothing there to click and the keyboard is the only way in.
A scan of a window like that comes back flagged `nothing_clickable` rather than offering a
target that cannot work.

No new dependencies for the working part: UI Automation comes through `comtypes`, which
was already here for SAPI, and input goes through `SendInput` in `ctypes`.

Two limits worth knowing. Windows silently refuses input from an unelevated process to
any window running as administrator - the tree reads perfectly and every click does
nothing. And UI Automation only reaches the desktop from the signed-in session, the same
way an audio device does, so a service in session 0 sees nothing.

## What leaves this machine

Nothing, unless you ask for it. At startup JARVIS prints exactly what each stage is doing:

```
ears: whisper (local) -> voice: auto (local). Nothing leaves this machine.
```

Two backends are remote, both opt in, and both warn loudly when selected:

| Setting | Sends | To |
| --- | --- | --- |
| `stt.backend = "google"` | your raw microphone audio | Google |
| `tts.engine = "edge"` | every reply, as text | Microsoft |

One thing is not JARVIS's to promise: the `screenshot` tool hands a picture of your
screen to whatever agent is connected, and everything a tool returns goes wherever that
agent runs. Local model, local picture. Cloud model, and it is on someone else's server -
along with whatever happened to be on screen at the time. Nothing else here sends an
image, and `screen.send_image` is off by default for the same reason.

`tts.engine = "auto"` never selects `edge` - you have to name it, and install it:

```powershell
uv sync --extra edge     # better voice, but sends every reply to Microsoft
```

## Configuration

Everything is configurable three ways, each beating the last:

1. a config file - `config/jarvis.json`
2. environment variables, e.g. `JARVIS_STT_BACKEND`, `JARVIS_TTS_ENGINE`
3. command line flags

```powershell
jarvis config              # everything in effect, and where it came from
jarvis config --defaults   # just the built-in defaults
```

`config/defaults.json` lists every option with its default value. It is generated from
the code by `jarvis config --defaults --write`, and a test fails if the two drift - a
hand-written example goes stale the first time someone changes a default.

Copy the bits you want into `config/jarvis.json`; anything absent keeps its default.
JSON has no comments, so any key beginning with `_` is ignored and can be used to write
down why a setting is what it is:

```json
{
  "stt": {
    "_why": "GPU is free on this machine, and CPU is slow on long utterances",
    "whisper_model": "small.en",
    "whisper_device": "auto"
  }
}
```

`config/jarvis.toml.example` is the annotated version of the same settings, kept because
comments explain trade-offs better than a schema can. TOML is still accepted - the search
order is `config/jarvis.json`, `config/jarvis.toml`, `jarvis.json`, `jarvis.toml`, or
whatever `JARVIS_CONFIG` points at.

## Architecture

```
 mic thread ──▶ queue ──▶ STT ──▶ transcript ──▶ GET /heard (blocks)
      ▲                                                              │
      └──── muted while speaking ◀── speech thread ◀── POST /say ◀───┘ agent
```

| Module | Role |
| --- | --- |
| `cli.py` | Argument parsing, wiring, the `serve` / `say` / `next` / `status` / `config` / `look` / `click` / `mcp` commands |
| `service.py` | Owns the hardware, serves loopback HTTP |
| `transcript.py` | Append-only record with blocking reads |
| `client.py` | Client for the service, shared by the CLI and MCP |
| `mcp_server.py` | The tools an agent can call |
| `microphone.py` | Background capture, phrase splitting, mute |
| `vad.py` | Whether a buffer is speech: Silero, or loudness as a fallback |
| `stt.py` | Local Whisper transcription, with Google as an opt in |
| `tts.py` | Speech worker thread, SAPI and Edge backends, sentence splitting |
| `hotkey.py` | The global key that stops and starts listening |
| `screen.py` | Cutting the accessibility tree to numbered targets, and refusing stale ones |
| `uia.py` | UI Automation through comtypes: the only Windows-specific module |
| `hands.py` | Synthetic clicks and keystrokes, through SendInput |
| `marks.py` | The numbered boxes drawn onto a screenshot |
| `reap.py` | Clearing MCP servers that outlived their client |
| `echo.py` | Recognising JARVIS's own voice coming back |
| `config.py` | Defaults, TOML, environment |

Capture and speech are half duplex on purpose: with one microphone and no echo cancellation,
listening while speaking just means transcribing yourself. Audio recorded while JARVIS was
talking is dropped even if it arrives afterwards, and anything that slips through is compared
against what was just spoken. If it still hears itself, raise `audio.min_energy_threshold` or
`audio.echo_guard_seconds`.

[`DESIGN.md`](DESIGN.md) has the reasoning behind the less obvious choices.

## Development

```powershell
uv run pytest        # 317 tests, no hardware, model or network needed
uv run ruff check .
uv run ruff format .
```

## Example configuration

One real setup in full - hardware, the llama.cpp launcher it runs, and the local config
overrides: [`docs/example-configuration.md`](docs/example-configuration.md).

## License

MIT
