# Example configuration

One real setup, in full. Nothing here is required - JARVIS has no model of its own and does
not care which agent connects to it - but a working set of numbers is easier to adapt than a
list of options.

## Hardware

- **GPU** - RTX 4070 Ti, 12 GB
- **CPU** - Ryzen 7 7700X, 8 cores
- **RAM** - 32 GB DDR5-6000
- **Microphone** - Razer Seiren Mini, a supercardioid desk condenser

## Software

- **LLM** - Qwen3.6-35B-A3B IQ4_XS on llama.cpp, 128k context, `--n-cpu-moe 25`
- **Agent** - Cline CLI over MCP, `openai-compatible` provider at `127.0.0.1:8081`
- **STT** - `small.en` on CUDA, `int8_float16`
- **TTS** - SAPI, Microsoft George

VRAM is the binding constraint. With the model loaded there is about 127 MB spare, which is
why speech detection runs on the CPU: Silero costs 0.19% of one core and no VRAM, where a
GPU denoiser would have to be paid for out of `--n-cpu-moe`.

## The llama.cpp launcher

Sampling is tight on purpose. Choosing a tool is a one-right-answer decision with no
creative upside, and at `--temp 0.6` a plausible-but-wrong tool got picked often enough to
notice - deciding to speak and then calling the listen tool instead. `--top-k` and `--top-p`
are Qwen3's own recommendations rather than llama.cpp's looser 40 and 0.95.

```bat
@echo off
title Qwen3.6-35B agent server - one 128k slot - 127.0.0.1:8081

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
  --n-cpu-moe 25 ^
  -fa on ^
  -ub 2048 ^
  -ctk q8_0 -ctv q8_0 ^
  --temp 0.3 --top-k 20 --top-p 0.8 --min-p 0.05 ^
  --jinja ^
  --load-mode mlock ^
  --no-reasoning-preserve ^
  -c 131072 ^
  --parallel 1 ^
  --cache-reuse 256 ^
  --host 127.0.0.1 ^
  --port 8081

:: Brief pause to let the server spin up its port
timeout /t 2 /nobreak >nul
```

One thing to know if you copy this: `llama-server.exe` is called bare, relying on `cmd`
searching its own directory. That fails wherever `NoDefaultCurrentDirectoryInExePath` is
set, which some sandboxed shells do, and the error is the unhelpful `'llama-server.exe' is
not recognized as an internal or external command`. Writing `.\llama-server.exe` avoids it.

## Telling Cline the context size

Cline cannot discover the context window from a custom endpoint, so it has to be told. In
`providers.json`, `contextWindow` must match llama.cpp's `-c` or compaction fires too late:

```json
{ "contextWindow": 131072, "maxTokens": 32000 }
```

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
