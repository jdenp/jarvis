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
- **STT** - `small.en` on CUDA, `int8_float16`
- **TTS** - Kokoro `bm_george`, on the CPU

VRAM is the binding constraint. llama-server alone leaves 810 MB free on the 12 GB card, and
Whisper takes ~580 MB with ~340 MB of CUDA context on top - about 127 MB spare once everything
is up. That is why speech detection stays on the CPU: Silero costs 0.19% of one core and no
VRAM, where a GPU denoiser would have to be paid for out of `--n-cpu-moe`. Kokoro is there
for the same reason and it costs nothing to be - about 5x real time on this chip, so under
a second for a sentence, against ~300 MB of VRAM for latency nobody can hear.

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

One slot is right because there is one context in flight: the brain's. Nothing else is
evicting and refilling the KV cache between turns, so `--cache-reuse 256` does what it is
there for.

Context and `--n-cpu-moe` trade against each other: more context is more KV cache, and moving
another MoE layer off the GPU is how you pay for it. 100k loads in about 35 seconds here.

One thing to know if you copy this: `llama-server.exe` is called bare, relying on `cmd`
searching its own directory. That fails wherever `NoDefaultCurrentDirectoryInExePath` is
set, which some sandboxed shells do, and the error is the unhelpful `'llama-server.exe' is
not recognized as an internal or external command`. Writing `.\llama-server.exe` avoids it.

## Local overrides

`config/jarvis.json` on this machine, with the defaults left alone everywhere else:

```json
{
  "stt": {
    "whisper_model": "small.en",
    "whisper_device": "auto",
    "whisper_compute_type": "int8_float16"
  },
  "tts": {
    "engine": "kokoro",
    "kokoro_device": "cpu"
  },
  "service": {
    "_why": "`jarvis next` waits as long as it is told to, so a long poll is safe here and returns empty far less often",
    "max_wait_seconds": 240
  },
  "_log": "20GB. Nothing on this disk needs the space and a week of transcripts is worth more.",
  "log_max_mb": 20480
}
```

## The web app

On, which is the default, and reached from a phone with one command on this machine:

```powershell
tailscale serve --bg 8770
```

The rest of it - MagicDNS, the certificate, and why the plain tailnet IP will not do - is in
[Reaching it from your phone](../README.md#reaching-it-from-your-phone). Kokoro above is what
makes it work at all: the reply has to be rendered to a wav for the browser to play, and SAPI
and Edge cannot be rendered without playing them, so with those it speaks at the desk instead.

## Computer use

This started with [open-computer-use](https://github.com/QwenLM/open-computer-use), which
drives Windows through UI Automation. It did not work here, and the reason was not the
transport: `get_app_state` hands the model the whole accessibility tree, which for anything
real is hundreds of nodes of panes and static text. Qwen3.6-35B given the lot picked
plausible wrong elements and could not reliably interact with anything.

JARVIS does that job itself now, filtered - see
[Screen control](../README.md#screen-control). The tree never reaches the model; a
numbered list of the few dozen actionable elements does. On this machine:

```json
{ "screen": { "control": true } }
```

### Vision, and why it stays off

Nothing in the screen loop wants pixels. Targets come back as ids and labels, so a text-only
endpoint is missing nothing, and `logs/marks.png` is written on request for a human to look
at when a click goes somewhere unexpected. Two notes on that:

- `look_at_image` is the one tool that hands the model a picture, and there is no `--mmproj`
  in the launcher above, so on this setup it has nothing to read one with.
- Vision is available if wanted: there is an `mmproj` beside each GGUF, and
  `--mmproj <file> --no-mmproj-offload` loads it without touching VRAM - the ~860 MB of
  projector weights stay in system RAM and encoding runs on the CPU. The cost is a CPU
  encode per image, plus the image tokens, which are dynamic-resolution here and run to
  four figures for a full screen. `--image-max-tokens` caps that.

For screen control specifically it still is not worth turning on. The text list carries
exact labels and exact geometry; a vision model reading a marked screenshot is inferring
both, and pays seconds and a thousand context tokens to do it worse. The case that would
genuinely need pixels is an application with no usable accessibility tree at all - a
game, a canvas, a remote desktop window - and a picture does not solve that one either,
because with no targets there is nothing to number. See DESIGN.md.

### Notes

- UI Automation works only from the signed-in desktop session, the same way an audio
  device does. A process in session 0 cannot drive the desktop, so if screen control goes
  dead, check the session before anything else.
- Windows blocks input from an unelevated process to any window running as administrator.
  The tree still reads perfectly and every click silently does nothing.
- `jarvis look` and `jarvis click` are the fallback and the way to try it by hand:
  `jarvis look "outlook" --matching reply --marks` prints the numbers and writes the
  marked screenshot. `screen.control` gates the brain's tools, not these.
- The scan expires after 60s and is re-checked against what is under the pointer before
  anything is pressed, so a stale number is refused rather than clicked.
