# JARVIS

> **J**ust **A** **R**ather **V**ery **I**ntelligent **S**ystem

A local voice service. It listens on your microphone and hands everything it hears to
whatever agent is connected. That agent decides what was meant for it, and speaks back
through your speakers.

JARVIS has no model of its own - it is ears and a mouth, and the agent is the brain.

## Features

- **Fully local.** Whisper transcribes on this machine and the voice is the offline Windows
  one. Nothing leaves the machine, and JARVIS prints a line at startup saying so.
- Runs on CPU with no setup, or on the GPU for much quicker transcription. See
  [Speech recognition](#speech-recognition).
- **Blocking reads, not polling.** `wait_for_speech` returns the instant you finish a
  sentence, so an agent waits on it rather than asking repeatedly.
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

## Requirements

- Python 3.12+
- A microphone

## Setup

```powershell
uv sync
copy config\jarvis.toml.example config\jarvis.toml   # optional, all values have defaults
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
well, and the limits worth knowing.

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

Four tools appear: `wait_for_speech()`, `check_for_speech()`, `say(text)` and
`voice_status()`.
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
  waits until it next calls `wait_for_speech`. Cooperative, not preemptive, and no transport
  changes that.
- **A quiet session returns empty results.** `wait_for_speech` blocks for
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
| `cli.py` | Argument parsing, wiring, the `serve` / `say` / `next` / `mcp` commands |
| `service.py` | Owns the hardware, serves loopback HTTP |
| `transcript.py` | Append-only record with blocking reads |
| `client.py` | Client for the service, shared by the CLI and MCP |
| `mcp_server.py` | The tools an agent can call |
| `microphone.py` | Background capture, phrase splitting, mute |
| `vad.py` | Whether a buffer is speech: Silero, or loudness as a fallback |
| `stt.py` | Local Whisper transcription, with Google as an opt in |
| `tts.py` | Speech worker thread, SAPI and Edge backends, sentence splitting |
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
uv run pytest        # 165 tests, no hardware, model or network needed
uv run ruff check .
uv run ruff format .
```

## Example configuration

One real setup in full - hardware, the llama.cpp launcher it runs, and the local config
overrides: [`docs/example-configuration.md`](docs/example-configuration.md).

## License

MIT
