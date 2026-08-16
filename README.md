# JARVIS

> **J**ust **A** **R**ather **V**ery **I**ntelligent **S**ystem

A local voice service. It listens on your microphone and hands everything it hears to
whatever agent is connected. That agent decides what was meant for it, and speaks back
through your speakers.

JARVIS has no model of its own - it is ears and a mouth, and the agent is the brain.

## Features

- **Fully local.** Whisper transcribes on this machine and the voice is the offline Windows
  one. Nothing leaves the machine, and JARVIS prints a line at startup saying so.
- Runs on CPU with no setup, or on the GPU for four to six times quicker transcription -
  which matters more than it sounds, because CPU transcription time grows sharply with
  how long you spoke. See [Transcription speed](#transcription-speed-accuracy-and-vram).
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
- **The latency floor is `audio.pause_threshold`**, 1.7s by default: that much silence
  before JARVIS decides your sentence ended, plus ~0.3s of Whisper and a 0.8s settle
  window. Measured transport cost from transcription to the agent is ~0.0s, so that is
  the only knob worth touching. It is set high deliberately - being cut off mid sentence
  is worse than waiting.

## Transcription speed, accuracy and VRAM

**On CPU, transcription time is badly non-linear in how long you spoke.** This is the
single most surprising thing about running it, and the reason the GPU is worth more than
the headline numbers suggest. Measured on an RTX 4070 Ti and an 8-core CPU with
`small.en`:

| you spoke for | CPU | CUDA |
| --- | --- | --- |
| 5.5s | 0.99s | 0.16s |
| 11.0s | 1.00s | 0.15s |
| 22.1s | **12.06s** | 3.12s |
| 44.1s | **23.08s** | 4.72s |

A short sentence costs about a second on CPU, which is fine. Ask something that takes
twenty seconds to say and you wait twelve seconds for it to be transcribed - and the
penalty lands hardest exactly when you have just explained something at length and are
most expecting an answer. The GPU is four to six times quicker at every length, and
barely notices a long utterance.

If you are on CPU and the delay feels inconsistent, this is almost certainly why: it
tracks utterance length, not luck. `audio.pause_threshold` also lets you ramble without
being cut off, which makes long utterances more likely.

Note that CPU transcription competes with anything else using the CPU - a local LLM
offloading layers with `--n-cpu-moe`, for instance - so the delay grows again while the
agent is thinking.

### The other two

**Accuracy comes from the model, not the device.** `small.en` is markedly better than
`base.en` at proper nouns and at accents the model was not weighted towards - which is
what turns "jarvis" into Jovis, Darvus or Java's. CUDA does not transcribe more
accurately, it makes the larger model affordable in time.

**Most of the VRAM is the CUDA context, not the weights.** `base.en` in int8 is about
75 MiB of weights against a 339 MiB total, so choosing a smaller model to save VRAM
barely helps - roughly 265 MiB is the price of touching the GPU at all.

| `whisper_model` | `whisper_device` | short utterance | VRAM |
| --- | --- | --- | --- |
| `base.en` | `cpu` | 0.32s | none |
| `base.en` | `cuda` | 0.07s | 339 MiB |
| `small.en` | `cpu` | 0.98s | none |
| `small.en` | `cuda` | 0.18s | 563 MiB |

**The shipped default is `base.en` on `cpu`** - no VRAM, no CUDA install, and the
mildest version of the scaling problem above. To use the GPU:

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
CUDA install or a GPU with no room falls back to CPU rather than silently returning
nothing.

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

## Example configuration

One machine this runs on, as a starting point rather than a recommendation. The
interesting constraint is that a 35B model and a speech model are sharing 12 GB.

| | |
| --- | --- |
| GPU | RTX 4070 Ti, 12 GB |
| CPU | Ryzen 7 7700X, 8 cores |
| RAM | 32 GB DDR5-4800 |
| Agent | Cline CLI, talking to JARVIS over MCP |
| LLM | Qwen3.6-35B-A3B IQ4_XS on llama.cpp, 128k context |

**VRAM is the binding constraint, and Whisper loses.** The LLM takes almost all of the
12 GB, leaving roughly 900 MB. `small.en` on the GPU wants 563 MB of that, so it fits
only because the LLM offloads 25 MoE layers to CPU (`--n-cpu-moe 25`) to make room. The
alternative - `small.en` on CPU - is free in VRAM but costs 0.66s on a short utterance
and far more on a long one, which is why the GPU won.

`config/jarvis.json`:

```json
{
  "stt": {
    "whisper_model": "small.en",
    "whisper_device": "auto",
    "whisper_compute_type": "int8_float16"
  }
}
```

**Sampling matters as much as the prompt.** llama.cpp defaults, and Qwen3's own
recommended `--temp 0.6`, are tuned for chat. Choosing a tool is a one-right-answer
decision with no creative upside, and at 0.6 a plausible-but-wrong tool gets sampled
often enough to notice - the agent deciding to speak and then calling the listen tool
instead. Tightening it fixed more than several rounds of rewording the instructions did:

```
--temp 0.2 --top-k 20 --top-p 0.8 --min-p 0.05 --jinja
```

`--jinja` uses the model's own chat template, which is what parses tool calls on the
OpenAI-compatible endpoint.

**What it feels like.** About 1.7s of silence before a phrase is considered finished,
~0.2s to transcribe, 0.8s settle, then however long the agent takes to think - so
roughly three seconds from finishing a sentence to the agent having it, and the model's
own latency on top of that.

## License

MIT
