# JARVIS

> **J**ust **A** **R**ather **V**ery **I**ntelligent **S**ystem

A local voice service. It listens on your microphone and hands everything it hears to
whatever agent is connected. That agent decides what was meant for it, and speaks back
through your speakers.

JARVIS has no model of its own - it is ears and a mouth, and the agent is the brain.

## Features

- **Fully local.** Whisper transcribes on this machine and the voice is the offline Windows
  one. Nothing leaves the machine, and JARVIS prints a line at startup saying so.
- **Blocking reads, not polling.** `wait_for_speech` returns the instant you finish a
  sentence, so an agent waits on it rather than asking repeatedly.
- MCP server, so Cline and friends see the microphone as tools they can call
- A plain CLI for everything else
- **No wake word.** Everything heard is passed on and the agent judges what was addressed
  to it, so you do not have to say "jarvis" before every reply in a conversation
- `check_for_speech` for steering mid task, since nothing can preempt an agent
- Half duplex with an echo guard, so JARVIS never transcribes its own voice
- Append-only transcript with monotonic ids, so nothing is missed across a reconnect

## Requirements

- Python 3.12+
- A microphone

## Setup

```powershell
uv sync
copy jarvis.toml.example jarvis.toml   # optional, all values have defaults
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

Just talk. The name is stripped when you use it but is not required - everything heard is
passed to the agent, which decides what was aimed at it. Set `wake.required = true` to
require the name again. Logs rotate in `logs/jarvis.log`; everything heard is appended to
`logs/heard.jsonl`.

## Connecting an agent

Hand the agent [`jarvis.md`](jarvis.md) as context - it explains the tools, how to speak
well, and the limits worth knowing.

For Cline, add to your MCP settings:

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/jarvis", "jarvis", "mcp"]
    }
  }
}
```

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
- **The latency floor is `audio.pause_threshold`**, 2.0s by default: that much silence
  before JARVIS decides your sentence ended, plus ~0.3s of Whisper and a 0.8s settle
  window. Measured transport cost from transcription to the agent is ~0.0s, so that is
  the only knob worth touching. It is set high deliberately - being cut off mid sentence
  is worse than waiting.

## Transcription accuracy, speed and VRAM

The three trade against each other, so the defaults are conservative and this is the
knob most worth touching. Measured on this machine (RTX 4070 Ti) with five seconds of
speech, each in a fresh process so the CUDA context is not shared:

| `whisper_model` | `whisper_device` | warm transcribe | VRAM |
| --- | --- | --- | --- |
| `base.en` | `cpu` | 0.32s | none |
| `base.en` | `cuda` | 0.07s | 339 MiB |
| `small.en` | `cpu` | 0.98s | none |
| `small.en` | `cuda` | 0.18s | 563 MiB |

Two things fall out of that.

**Accuracy comes from the model, not the device.** `small.en` is markedly better than
`base.en` at proper nouns and at accents the model was not weighted towards - which is
what turns "jarvis" into Jovis, Darvus or Java's. CUDA does not make it more accurate,
it makes the larger model affordable in time.

**Most of the VRAM is the CUDA context, not the weights.** `base.en` in int8 is about
75 MiB of weights against a 339 MiB total, so choosing a smaller model to save VRAM
barely helps - roughly 265 MiB is the cost of using the GPU at all.

Whether it is worth it depends on what else the GPU is doing. On a machine also running
a local LLM, 0.2s off a pipeline whose delay is dominated by `audio.pause_threshold`
(2.0s) is a poor trade for several hundred megabytes. On an idle GPU it is free.

**The shipped default is `base.en` on `cpu`** - no VRAM, no CUDA install, 0.32s. If
accuracy matters more than latency and the GPU is spoken for, `small.en` on `cpu` is the
middle ground: the better model for nothing but about 0.66s more per utterance, which
against a 2.0s `pause_threshold` takes the round trip from roughly 3.1s to 3.8s.

To use the GPU instead:

```powershell
uv sync --extra cuda    # CUDA runtime as pip packages, no system install needed
```

then in `jarvis.toml`:

```toml
[stt]
whisper_model = "small.en"      # or base.en for half the VRAM
whisper_device = "auto"         # falls back to cpu if CUDA will not load
whisper_compute_type = "int8_float16"
```

`auto` proves the device works with a real inference before accepting it, so a broken
CUDA install falls back to CPU rather than silently returning nothing.

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

1. `jarvis.toml` in the project root - see `jarvis.toml.example` for the full list
2. environment variables, e.g. `JARVIS_STT_BACKEND`, `JARVIS_TTS_ENGINE`
3. command line flags

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
| `microphone.py` | Background capture, calibration, mute |
| `stt.py` | Local Whisper transcription, with Google as an opt in |
| `tts.py` | Speech worker thread, SAPI and Edge backends, sentence splitting |
| `wake.py` | Wake word matching, exact and approximate |
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
uv run pytest        # 147 tests, no hardware, model or network needed
uv run ruff check .
uv run ruff format .
```

## License

MIT
