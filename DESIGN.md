# JARVIS - design notes

Why things are the way they are, for whoever works on this next. `jarvis.md` is the
separate, shorter file you hand to an agent that is *using* JARVIS rather than changing it.

## What this is

Ears and a mouth, and nothing else. An agent on the other end is the brain.

```
mic thread ──▶ queue ──▶ STT ──▶ wake word ──▶ transcript ──▶ GET /heard (blocks)
     ▲                                                              │
     └──── muted while speaking ◀── speech thread ◀── POST /say ◀───┘ agent
```

There used to be a standalone assistant here - its own LLM, a skills registry with tool
calling, a persona. It was removed deliberately, not abandoned: two things that can answer
the user is one too many, and everything it did the connected agent already does better.
If you are tempted to add a model back, add it on the agent's side of the socket.

`jarvis serve` is a daemon rather than three separate programs for one reason: **only one
process can own the microphone.** `jarvis say` from an agent's terminal has to mute the same
microphone that is listening, or JARVIS transcribes itself. So the daemon holds the hardware
and everything else - the CLI and the MCP server both - is a loopback HTTP client.

## The interrupt is a blocking read

`GET /heard?since=N&wait=55` returns immediately if there is anything after `N`, and
otherwise blocks on a `threading.Condition` that `Transcript.add` notifies. An agent calls
`wait_for_speech` once and it returns the instant a sentence lands. No polling, no timer, no
tokens spent asking "anything yet?".

Ids are monotonic and survive restarts - `Transcript._resume` reads the last id out of the
JSONL rather than replaying it - so a client holding a cursor never misses or repeats an
utterance across a reconnect.

Two limits, neither fixable by changing the transport:

- **Nothing preempts an agent mid-turn.** There is no external interrupt for an agent loop
  already running tools. Speech waits until the agent next chooses to listen. Cooperative
  by nature, which is what `check_for_speech` is for: a non-blocking peek the agent is
  told to make between the steps of a long task, so a change of mind reaches it before it
  has finished doing the wrong thing.
- **The latency floor is whatever `pause_threshold` is set to**, currently 1.7s, plus
  ~0.3s of Whisper and a 0.8s settle window. Measured cost from transcript to agent is
  ~0.0s, so optimising the transport is pointless. It is set high deliberately: being
  cut off mid sentence is a worse experience than waiting, and the two trade directly
  against each other.

`max_wait_seconds` defaults to 55 because agent clients time out tool calls (Cline's is
around 60s). The tool takes a timeout, returns empty on expiry, and the agent calls again -
still not polling, just a long poll.

## Local is the default, and that is the point

Every stage runs on this machine unless explicitly told otherwise. Two backends are remote
and both are opt in: `stt.backend = "google"` uploads raw microphone audio, `tts.engine =
"edge"` sends every reply to Microsoft. `tts.engine = "auto"` resolves to `sapi` and will
never reach for `edge` on its own.

If you add a backend that talks to a third party, give it `is_local = False` and log a
warning in its constructor. `privacy_report()` in `cli.py` reads that attribute to print the
startup line, so a new remote backend surfaces automatically rather than quietly.

## Design decisions worth not undoing

**Calibrate on the recognizer that listens.** The original code built a throwaway
`sr.Recognizer()` inside `calibrate()`, so the measured energy threshold was discarded and
calibration did nothing. One `Recognizer` and one `Microphone` now live for the session.

**Silence must never be the whole response.** An utterance with no wake word used to be
dropped at DEBUG, so it vanished without trace and looked identical to a hang. It logs at
INFO now, naming the word to use.

**Gate the microphone on when audio was recorded, not when it arrived.**
`listen_in_background` only hands a phrase over once the phrase *ends*. A mute flag checked
in the callback therefore says nothing about when the audio was captured: JARVIS speaks, the
listener is mid-phrase recording it, JARVIS stops, `unmute()` drains an empty queue, and only
*then* does the phrase arrive - unmuted, and containing JARVIS's own voice.

So `_on_audio` works backwards from the phrase length to when it started, and drops anything
overlapping the window in which JARVIS was speaking (plus `echo_guard_seconds` for the output
buffer and room echo). `EchoGuard` in `echo.py` is the second line of defence, comparing new
transcripts against what was recently spoken - hearing yourself is lossy and usually clips
the start, so it matches on containment or a similarity ratio rather than equality.

There is also a floor under the calibrated energy threshold. A quiet room calibrates to
single digits, which is sensitive enough to hear the speakers at all, and no amount of gating
helps if the mic is straining to pick up its own output.

**Load the Whisper model once, and prove the device works before trusting it.**
speech_recognition's `recognize_faster_whisper` builds a fresh `WhisperModel` on every call,
so using it means reloading weights from disk per utterance. Worse, `WhisperModel.transcribe`
returns a *generator* - constructing a model on a broken CUDA install succeeds and the
failure only lands on the first inference, one silent `None` per utterance forever. So
`_load()` runs a real warm-up inference on a second of noise and falls back to CPU if that
throws. A machine with a GPU but no CUDA runtime DLLs (`cublas64_12.dll` and friends) hits
exactly this, and the fallback is what makes it work there at all. `base.en` on CPU
transcribes a short phrase in under 0.3s.

**Transcription cost is non-linear in utterance length, and that is a CPU problem.**
Measured with `small.en`: 5.5s of speech takes 0.99s on CPU and 0.16s on CUDA, but 22s of
speech takes 12.06s on CPU against 3.12s on CUDA. A short sentence is fine either way; a
long one is not. The penalty lands exactly when someone has explained something at length
and is most expecting an answer, so on CPU it reads as the assistant being erratic rather
than slow. If the delay ever needs chasing, measure against utterance length before
touching anything else.

**Build TTS backends on the thread that uses them.** Both SAPI (COM apartment affinity) and
pygame hold thread-affine resources. `SpeechEngine` takes a factory and calls it inside the
worker. Building on the main thread and calling from the worker gets you silence or a hang.

**Count pending utterances, do not flag idleness.** `SpeechEngine` tracks a count under a
`Condition` rather than setting an idle `Event`. A flag races: the worker can see an empty
queue and mark itself idle in the window between `say()` incrementing and `say()` enqueueing,
so `wait()` returns while an utterance is still to come.

## Swapping a component

Each is a Protocol or a factory, so a replacement only has to match the shape:

- speech to text: `Transcriber` in `stt.py`, add to `build_transcriber`
- speech: `Speaker` in `tts.py`, add to `build_speaker`. Piper is the obvious next one -
  offline like SAPI but a far better voice, so it could become the `auto` default without
  giving up the local guarantee

## Still missing

- Wake word detection on audio rather than transcript, e.g. openWakeWord. The current gate
  transcribes everything first, so every cough costs a Whisper inference. Local, so it is
  wasted CPU rather than a privacy problem, but still wasteful
- Speaker identification, so a room with two people in it does not confuse the agent
- Nothing tells the agent that speech arrived while it was busy. It only finds out when it
  next calls `wait_for_speech`. An MCP notification could improve that, if clients honour it
