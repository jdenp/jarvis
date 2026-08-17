# JARVIS - design notes

Why things are the way they are, for whoever works on this next. `jarvis.md` is the
separate, shorter file you hand to an agent that is *using* JARVIS rather than changing it.

## What this is

Ears and a mouth, and nothing else. An agent on the other end is the brain.

```
mic thread ──▶ queue ──▶ STT ──▶ transcript ──▶ GET /heard (blocks)
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
- **The latency floor is whatever `pause_threshold` is set to**, currently 1.5s, plus
  Whisper and a 0.8s settle window. Measured cost from transcript to agent is ~0.0s, so
  optimising the transport is pointless. It is set high deliberately: being cut off mid
  sentence is a worse experience than waiting, and the two trade directly against each
  other. The ceiling is `phrase_time_limit` - see below.

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

**No wake word, and no fuzzy matching of one.** It went in three stages: required, then
optional, then removed. Once the agent was deciding for itself what was meant for it, the
matching only ever produced false positives - and `hotwords="JARVIS"` biased Whisper's
decoder towards producing the word, so it manufactured "JARVIS" out of room noise and put
it in the transcript. Passing everything through verbatim is both simpler and more
accurate; the agent is better placed to judge than a string match.

**Silence must never be the whole response.** An utterance with no wake word used to be
dropped at DEBUG, so it vanished without trace and looked identical to a hang. It logs at
INFO now, naming the word to use.

**Own the capture loop, so a phrase can end while the room is still noisy.**
`speech_recognition` ends a phrase after `pause_threshold` of *consecutive* buffers below the
energy threshold, and resets that count on any single buffer above it. One keyboard click a
second therefore holds a phrase open indefinitely, and the only thing that ends it is
`phrase_time_limit` - so a sentence spoken into a noisy room reaches the agent a minute
later, or not until the speaker has given up.

`PhraseEnd` in `microphone.py` still waits for a whole `pause_threshold` of quiet, but lets
it be interrupted: the window is widened by `pause_quiet_fraction` and the quiet inside it
only has to add up. Requiring a *fraction of a fixed window* instead - which is how this was
first written - silently shortens the pause, turning 1.5s of patience into 1.28s, on the one
setting that has been tuned by ear more than any other. Widening keeps `pause_threshold`
meaning what it says, so the fraction buys noise tolerance rather than spending patience.

0.85 is measured, not guessed. Rendered through the same SAPI voice and replayed through the
real threshold dynamics, 0.85 ended one sentence in five early and 0.8 ended two, for 0.06s
more noise tolerance - so the knee is around 0.85. Both figures come from synthesised speech,
whose pauses at punctuation are longer and more regular than a real speaker's, so treat them
as an ordering rather than a measurement of your own voice.
`scripts/measure-pause-tolerance.py` regenerates the table if you want to argue with it.

`phrase_time_limit` stays at 60s as the last resort. Reaching it means waiting a minute, but
the alternative is cutting someone off mid sentence, and very little gets there now.

**A pause is measured in frames that are not speech, not in quiet ones.** Loudness cannot
tell a footstep from a word, so any tolerance rule is guessing. `vad.py` runs Silero, a 1.2MB
network that scores each 32ms frame, and the whole predicate in `_run` is that one boolean -
which is the only reason swapping it was a small change.

It earns its place on measurements rather than reputation. Thumps as loud as speech score
0.006 and never cross the 0.5 cutoff; the same sentence 24dB quieter scores the same as the
original, so it also removes the need to raise your voice at a desk mic. Cost is 62us per
32ms frame on a 7700X - 514x real time, 0.19% of one core - and no VRAM, which matters here
because the GPU has ~127MB spare with the local model loaded. onnxruntime and the model both
arrive with faster-whisper, so it adds no dependency.
`scripts/measure-noise-rejection.py` shows the difference it makes: with footsteps twice a
second the loudness rule still copes, and between 3 and 5 a second the phrase never ends at
all, while Silero delivers the same 4.10s every time.

Two things it does not fix. A television with people talking on it is speech by any honest
measure, and only speaker identification would help. And Silero is level-independent, which
cuts both ways: a voice in the next room counts too.

One trap worth knowing: `sr.Microphone(sample_rate=None)` - the default - opens at the
*device's* rate, 44100 on this mic, and Silero only accepts 512 samples of 16 kHz. Nothing
errors; the frames are simply not the length it thinks, and the scores are junk. The rate is
passed explicitly now, and `_run` warns if a source turns up at anything else. The tell was a
probe reading 517 frames in 6s where 188 were due - exactly 44100/16000.

`EnergyDetector` is kept, as `audio.vad = "energy"` and as the automatic fallback if
onnxruntime will not load. It is also the only thing `calibration_seconds`,
`min_energy_threshold` and `dynamic_energy_threshold` still affect - in silero mode there is
no threshold to calibrate, and startup skips it.

Reading the device directly also makes the echo gate trivial. `listen_in_background` only
hands a phrase over once it *ends*, so a mute flag checked on delivery says nothing about
when the audio was recorded - JARVIS speaks, the listener is mid-phrase recording it,
`unmute()` drains an empty queue, and only then does the phrase arrive, unmuted and full of
JARVIS's own voice. Gating each buffer as it is read needs no such reasoning. `EchoGuard` in
`echo.py` is the second line of defence, comparing new transcripts against what was recently
spoken - hearing yourself is lossy and usually clips the start, so it matches on containment
or a similarity ratio rather than equality.

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
- Acoustic echo cancellation. `audio.listen_while_speaking` exists but is off by default,
  because without AEC the microphone hears the speakers and the only defence is the text
  comparison in `echo.py`. Real AEC would make barge-in workable
- Nothing tells the agent that speech arrived while it was busy. It only finds out when it
  next calls `wait_for_speech`. An MCP notification could improve that, if clients honour it
