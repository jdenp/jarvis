# Example configuration

One real setup, in full. Nothing here is required, but a working set of numbers is easier
to adapt than a list of options.

## Hardware

- **GPU** - RTX 4070 Ti, 12 GB
- **CPU** - Ryzen 7 7700X, 8 cores
- **RAM** - 32 GB DDR5-6000
- **Microphone** - Razer Seiren Mini, a supercardioid desk condenser

## Software

- **LLM** - Qwen3.6-35B-A3B IQ4_XS on llama.cpp, 100k context, `--n-cpu-moe 22`
- **Brain** - JARVIS's own loop against the same server, `brain.url = "http://127.0.0.1:8081/v1"`
- **Agent** - Cline CLI over MCP at the same endpoint, for when the microphone is handed over
  instead
- **STT** - `small.en` on CUDA, `int8_float16`
- **TTS** - SAPI, Microsoft Hazel

VRAM is the binding constraint. llama-server alone leaves 810 MB free on the 12 GB card, and
Whisper takes ~580 MB with ~340 MB of CUDA context on top - about 127 MB spare once everything
is up. That is why speech detection stays on the CPU: Silero costs 0.19% of one core and no
VRAM, where a GPU denoiser would have to be paid for out of `--n-cpu-moe`.

## The llama.cpp launcher

Sampling is tight on purpose. Choosing a tool is a one-right-answer decision with no
creative upside, and at `--temp 0.6` a plausible-but-wrong tool got picked often enough to
notice - deciding to speak and then calling the listen tool instead. `--top-k` and `--top-p`
are Qwen3's own recommendations rather than llama.cpp's looser 40 and 0.95.

```bat
@echo off
title Qwen3.6-35B agent server - one 100k slot - 127.0.0.1:8081

cd /d "%~dp0"
cd "llama-b10237-bin-win-cuda-12.4-x64"

:: The whole context in ONE slot, no parallelism.
:: Same KV allocation as a 4-slot build, just not divided up.
::
:: Sampling is deliberately tight. This is an agent, not a chat model: choosing
:: a tool is a one-right-answer decision with no creative upside, and at temp
:: 0.6 a plausible-but-wrong tool got sampled often enough to notice - deciding
:: to speak and then calling the listen tool instead. top-k and top-p are
:: Qwen3's own recommendations rather than llama.cpp's looser 40 and 0.95.
:: Raise temp back towards 0.6 if using this for prose rather than tools.
::
:: --jinja uses the model's own chat template, which is what parses tool calls
:: properly on the OpenAI-compatible endpoint.
::
:: Comments cannot go inside the ^ continuation below - batch treats them as
:: arguments and the command breaks.
llama-server.exe -m "..\Qwen3.6-35B-A3B-IQ4_XS-4.19bpw.gguf" ^
  -ngl 99 ^
  --n-cpu-moe 22 ^
  -fa on ^
  -ub 2048 ^
  -ctk q8_0 -ctv q8_0 ^
  --temp 0.4 --top-k 20 --top-p 0.8 --min-p 0.05 ^
  --jinja ^
  --load-mode mlock ^
  --no-reasoning-preserve ^
  -c 100000 ^
  --parallel 1 ^
  --cache-reuse 256 ^
  --host 127.0.0.1 ^
  --port 8081

:: Brief pause to let the server spin up its port
timeout /t 2 /nobreak >nul
```

One slot is right because there is one context in flight: the brain's. The KV cache is not
being evicted and refilled by an agent prompt and a JARVIS prompt taking turns, so
`--cache-reuse 256` does what it is there for. The section below about telling a coding agent
when to compact is history - it applied when an agent over MCP held the loop.

Context and `--n-cpu-moe` trade against each other: more context is more KV cache, and moving
another MoE layer off the GPU is how you pay for it. 100k loads in about 35 seconds here.

One thing to know if you copy this: `llama-server.exe` is called bare, relying on `cmd`
searching its own directory. That fails wherever `NoDefaultCurrentDirectoryInExePath` is
set, which some sandboxed shells do, and the error is the unhelpful `'llama-server.exe' is
not recognized as an internal or external command`. Writing `.\llama-server.exe` avoids it.

## Telling Cline when to compact

Cline cannot discover the context window from a custom endpoint, so `contextWindow` in
`providers.json` has to tell it, and it compacts at 0.9 of whatever it is told. That figure
has to be chosen from both ends. Too high and answers degrade before it ever compacts; too
low and it compacts constantly, and every compaction is a chance to be interrupted:

```json
{ "contextWindow": 90112, "maxTokens": 32000 }
```

88k triggers compaction at about 81k tokens. Adding `maxTokens` for the reply comes to 113k,
which still fits the server's 131,072 with room to spare - the ceiling worth watching, since
`contextWindow` alone exceeding `-c` means requests overflow the server rather than being
compacted.

Compaction is the fragile part of a voice session. It runs as a hub command against a timeout
hardcoded at 30 seconds, with no setting or environment variable behind it. In
`hub-daemon.log` the calls that land take 21-32ms, so when one fails at 30s the command was
never serviced rather than slow - and a `converse()` long poll holding a tool call open
for up to `max_wait_seconds` is the obvious suspect. Compacting less often is the cheap
mitigation.

## Local overrides

`config/jarvis.json` on this machine, with the defaults left alone everywhere else:

```json
{
  "stt": {
    "whisper_model": "small.en",
    "whisper_device": "auto",
    "whisper_compute_type": "int8_float16"
  },
  "service": {
    "_why": "Cline is configured with timeout 3600, so a long poll is safe and returns empty far less often",
    "max_wait_seconds": 240
  }
}
```

## Computer use, on the same agent

This started with [open-computer-use](https://github.com/QwenLM/open-computer-use), an
MCP server driving Windows through UI Automation. It did not work here, and the reason
was not the transport: `get_app_state` hands the model the whole accessibility tree,
which for anything real is hundreds of nodes of panes and static text. Qwen3.6-35B given
the lot picked plausible wrong elements and could not reliably interact with anything.

JARVIS does that job itself now, filtered - see
[Screen control](../README.md#screen-control). The tree never reaches the model; a
numbered list of the few dozen actionable elements does. On this machine:

```json
{ "screen": { "control": true } }
```

### The screenshot problem, in hindsight

The unresolved part used to be Cline sending a 1.39 MB base64 PNG to an endpoint with no
vision, there being no `--mmproj` in the launcher above. Two notes on that now:

- Nothing in the JARVIS loop wants pixels. Targets come back as ids and labels, so the
  text-only endpoint is missing nothing. `logs/marks.png` is written on request, for a
  human to look at when a click goes somewhere unexpected. `screen.send_image` is on by
  default and is the first thing to turn off against an endpoint with no vision - it is a
  megabyte of payload for something unreadable. The brain sends no images at all.
- Vision is available if wanted: there is an `mmproj` beside each GGUF, and
  `--mmproj <file> --no-mmproj-offload` loads it without touching VRAM - the ~860 MB of
  projector weights stay in system RAM and encoding runs on the CPU. The cost is a CPU
  encode per image, plus the image tokens, which are dynamic-resolution here and run to
  four figures for a full screen. `--image-max-tokens` caps that.

For screen control specifically it still is not worth turning on. The text list carries
exact labels and exact geometry; a vision model reading a marked screenshot is inferring
both, and pays seconds and a thousand context tokens to do it worse. The case that would
genuinely need pixels is an application with no usable accessibility tree at all - a
game, a canvas, a remote desktop window - and `send_image` does not solve that one
either, because with no targets there is nothing to number. See DESIGN.md.

The Cline side of it was never solved. `supportsImages` is the right flag but belongs to
a model entry rather than a provider, and setting it flat in `providers.json` achieves
nothing - Cline drops keys outside that file's schema on the next save, without
complaint. Where it persists the per-model flag has not been worked out here.

### Notes

- UI Automation works only from the signed-in desktop session, the same way an audio
  device does. A process in session 0 cannot drive the desktop, so if screen control goes
  dead, check the session before anything else.
- Windows blocks input from an unelevated process to any window running as administrator.
  The tree still reads perfectly and every click silently does nothing.
- `jarvis look` and `jarvis click` are the fallback and the way to try it by hand:
  `jarvis look "outlook" --matching reply --marks` prints the numbers and writes the
  marked screenshot. `screen.control` gates the agent's tools, not these.
- The scan expires after 60s and is re-checked against what is under the pointer before
  anything is pressed, so a stale number is refused rather than clicked.
