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

- **LLM** - Qwen3.6-35B-A3B IQ4_XS on llama.cpp, 192k context, `--n-cpu-moe 26`
- **Agent** - Cline CLI over MCP, `openai-compatible` provider at `127.0.0.1:8081`
- **STT** - `small.en` on CUDA, `int8_float16`
- **TTS** - SAPI, Microsoft Hazel

VRAM is the binding constraint. At these settings llama-server accounts for about 8.1 GB of
the 12 GB card - measured as the jump in `nvidia-smi` across a start - and Whisper wants
another ~580 MB with ~340 MB of CUDA context on top. That is why speech detection stays on
the CPU: Silero costs 0.19% of one core and no VRAM, where a GPU denoiser would have to be
paid for out of `--n-cpu-moe`.

## The llama.cpp launcher

Sampling is tight on purpose. Choosing a tool is a one-right-answer decision with no
creative upside, and at `--temp 0.6` a plausible-but-wrong tool got picked often enough to
notice - deciding to speak and then calling the listen tool instead. `--top-k` and `--top-p`
are Qwen3's own recommendations rather than llama.cpp's looser 40 and 0.95.

```bat
@echo off
title Qwen3.6-35B agent server - one 192k slot - 127.0.0.1:8081

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
  --n-cpu-moe 26 ^
  -fa on ^
  -ub 2048 ^
  -ctk q8_0 -ctv q8_0 ^
  --temp 0.4 --top-k 20 --top-p 0.8 --min-p 0.05 ^
  --jinja ^
  --load-mode mlock ^
  --no-reasoning-preserve ^
  -c 196608 ^
  --parallel 1 ^
  --cache-reuse 256 ^
  --host 127.0.0.1 ^
  --port 8081

:: Brief pause to let the server spin up its port
timeout /t 2 /nobreak >nul
```

Context and `--n-cpu-moe` trade directly against each other. More context is more KV cache,
and moving one more MoE layer off the GPU pays for it at roughly 528 MB a layer, going by the
expert tensor sizes llama.cpp prints while loading. 192k loads in about a minute here.

One thing to know if you copy this: `llama-server.exe` is called bare, relying on `cmd`
searching its own directory. That fails wherever `NoDefaultCurrentDirectoryInExePath` is
set, which some sandboxed shells do, and the error is the unhelpful `'llama-server.exe' is
not recognized as an internal or external command`. Writing `.\llama-server.exe` avoids it.

## Telling Cline the context size

Cline cannot discover the context window from a custom endpoint, so it has to be told. In
`providers.json`, `contextWindow` must match llama.cpp's `-c` or compaction fires too late:

```json
{ "contextWindow": 196608, "maxTokens": 32000 }
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

## Computer use, on the same agent

[open-computer-use](https://github.com/QwenLM/open-computer-use) is an MCP server that
drives Windows through UI Automation: nine tools for listing apps, reading an app's
accessibility tree, clicking, typing, scrolling and pressing keys. It pairs well with voice,
because "open the settings and turn on night light" becomes something the agent can actually
carry out rather than describe.

```powershell
npm i -g @qwen-code/open-computer-use
open-computer-use doctor        # confirms UI Automation is reachable
open-computer-use list-apps     # proves it can see your windows
```

In `cline_mcp_settings.json`, pointing at the bundled native binary rather than the npm shim
so Cline does not have to spawn a `.cmd`:

```json
{
  "open-computer-use": {
    "command": "C:\\Users\\short\\AppData\\Roaming\\npm\\node_modules\\@qwen-code\\open-computer-use\\dist\\windows\\amd64\\open-computer-use.exe",
    "args": [
      "mcp"
    ],
    "env": {},
    "timeout": 120
  }
}
```

### The screenshot problem, unresolved

`get_app_state` returns the accessibility tree **and a screenshot** - measured here, a text
block of 126 characters and a 1.39 MB base64 PNG. The endpoint above has no vision, there
being no `--mmproj`, so that image is pure cost.

The text half is all a text-only model needs: elements come back indexed and `click` takes
an `element_index`, so nothing in the loop wants pixels. Getting Cline to drop the image is
the unsolved part. `supportsImages` is the right flag, but in Cline 3.0.55 it belongs to a
model entry rather than to the provider:

```js
{ id, temperature?, maxTokens?, contextWindow?, inputPrice?, outputPrice?, supportsImages? }
```

Setting it flat in `providers.json` beside `contextWindow` achieves nothing - Cline drops
keys outside that file's schema the next time it saves, without complaint. Where it persists
the per-model flag has not been worked out here yet.

### Notes

- `doctor` reports that UI Automation works "when this process runs in the signed-in desktop
  session". A process in session 0 cannot drive the desktop, the same way it cannot reach an
  audio device - so if computer use goes dead, check the session before anything else.
- The CLI is a useful fallback and a good way to try tools by hand:
  `open-computer-use snapshot <app>` prints the tree as plain text with no screenshot at all.
- `get_app_state` must be called once per turn before interacting with an app.
- Windows blocks input from a process to any window running at a higher privilege, so a
  click on an elevated app silently does nothing while its tree still reads fine.
