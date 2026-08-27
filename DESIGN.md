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
otherwise blocks on a `threading.Condition` that `Transcript.add` notifies. An agent listens
once - `say(..., then="listen")` or `stay_silent` - and it returns the instant a sentence
lands. No polling, no timer, no tokens spent asking "anything yet?".

Ids are monotonic and survive restarts - `Transcript._resume` reads the last id out of the
JSONL rather than replaying it - so a client holding a cursor never misses or repeats an
utterance across a reconnect.

Two limits, neither fixable by changing the transport:

- **Nothing preempts an agent mid-turn.** There is no external interrupt for an agent loop
  already running tools. Speech waits until the agent next chooses to listen. Cooperative
  by nature, which is what `check_for_speech` is for: a non-blocking peek the agent is
  told to make between the steps of a long task, so a change of mind reaches it before it
  has finished doing the wrong thing.
- **The latency floor is whatever `pause_threshold` is set to**, currently 1.2s, plus
  about 0.2s of Whisper. Measured cost from transcript to agent is ~0.0s, so
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

0.85 was measured against the loudness predicate, where it cut one sentence in five short
and 0.8 cut two. Re-measured against Silero it cuts none, at any fraction down to 0.75 - a
gap between words is not speech but it is also nowhere near a whole `pause_threshold` of it,
so the fraction has far less to do now that the predicate is honest. 0.85 stays as the
conservative end of a range that no longer bites.

Both sets of figures come from synthesised speech, whose pauses at punctuation are longer and
more regular than a real speaker's, so read them as an ordering rather than a measurement of
your own voice. A genuine 1.2s hesitation mid sentence will still split the utterance, which
is what `pause_threshold` is for. `scripts/measure-pause-tolerance.py` regenerates the table.

`phrase_time_limit` stays at 60s as the last resort. Reaching it means waiting a minute, but
the alternative is cutting someone off mid sentence, and very little gets there now.

**JARVIS never speaks on its own initiative.** There used to be an `Acknowledger`: a
`threading.Timer` in the MCP server that spoke a canned phrase when `say()` had not been
called within a couple of seconds, so a slow answer did not sound like a crash. It was the
one place JARVIS decided *what* to say, and it is gone.

Two attempts to keep it are worth not repeating. Letting the agent supply the phrase through
`say(text, hold=...)` works mechanically but asks the wrong thing of a small model:
composing a follow-up that lands ten seconds later, with no context to attach it to, is a
judgement to make while it should be thinking about the actual question. And leaving the
canned timer in alongside an agent that now speaks first means two holding lines in a row
whenever the agent is a little slow, which is worse than either alone.

What replaced it is a rule in the instructions: if the answer is not immediate, say one line
before starting. The agent knows what it is about to do, and a timer never can. If the agent
forgets, there is silence - and silence is the honest signal that the agent forgot, rather
than something JARVIS papers over.

Note the constraint that shaped this: an agent only acts *between* tool calls, so nothing
told to it can make it speak from *inside* a slow one. Anything that tries to fill that gap
has to be JARVIS talking, which is the thing being removed.

**The loop is closed by the tool, not by the agent's memory.** Moving the lead-in rule into
the result fixed the silence before slow work, and left the other half untouched: the agent
answered, then ended its turn, hanging up on someone still sitting at the microphone.
Instructions could not reach it. `jarvis.md`, the server instructions and the `say()` result
all said "always go straight back to listening", two of them in capitals, and it
happened anyway - because it is a thing to remember at the end of a turn, and the end of a
turn is exactly where a model stops remembering.

So `say()` does it instead. It takes a required `then`: `then="listen"` speaks and then
performs the blocking read itself, returning the utterance in the same result, and
`then="keep_working"` speaks and returns at once for the lead-in. Answering *is* listening,
so there is no second call to forget. The argument has no default, so the schema rejects a
call that omits it - the model is made to state which of the two it is doing, and the fork
is one the lead-in rule already demanded, so it costs no judgement that was not already owed.

`stay_silent` covers what is left: entering the conversation, and listening again after
deciding to stay silent. Called with a question still unanswered it bounces once - returns
immediately without listening, naming what went unanswered - and clears the debt on the way
out, so the next call goes through whichever way the agent decides. That last part is the
whole difference from the version this repo already tried and removed, which blocked until
`say()` was called and deadlocked against an agent that had correctly kept quiet. One cheap
round trip is a cost worth paying; a hang is not.

That left one failure, and the first live session found it immediately: the agent heard
"Hello", wrote "Hello sir. How can I help?" into its own reply text, and called
`stay_silent`. Nothing was spoken. Closing the loop after `say()` does nothing about an
agent that never reaches `say()`, and the listening tool was the escape hatch -
argumentless, and indistinguishable from a correct decision to keep quiet.

**Silence has to be justified, and one of the justifications is checkable.**
`stay_silent` now takes a required `because`, one of
`starting_to_listen`, `not_aimed_at_me`, `sounded_cut_off`, `already_spoke_my_reply`. The
first three are judgements only the agent can make and are always honoured. The fourth is a
claim about the world, and the server knows whether it is true: `unanswered` holds the last
utterance heard and is cleared only by `say(..., then="listen")`, so a claim to have replied
with nothing behind it is refused, unlistened, and sent back to `say()`.

That value has to be in the list. An agent that has written its reply out believes it
answered, so `already_spoke_my_reply` is the honest thing for it to say - and it is exactly
the belief that needs contradicting. Take it out and the agent picks one of the unfalsifiable
three instead, and nothing catches anything.

It also cannot wedge a session, which is the constraint every previous attempt failed: the
refusal is conditional on the reason, so `not_aimed_at_me` always goes through. The escape is
in the enum rather than in a retry counter, and an agent that really was being talked over
gets past in one call. A lead-in does not settle the debt either - `then="keep_working"` was
the agent's own statement that this was not the answer, so "let me have a look" followed by
silence is still caught.

What is left unenforceable is narrower than it was: an agent that writes its reply as text
and then honestly reports `not_aimed_at_me` still gets through. That is a lie rather than a
lapse, and a lie is a much harder thing for a model to do by accident than forgetting. Rule 1
stays shouted in the instructions for the rest.

**Name the decision, not the mechanism.** These two tools were `say` and `wait_for_speech`,
and the second name stopped being true the moment `say(..., then="listen")` became the
ordinary way to hear someone. It read as the canonical listening primitive while actually
being the minority path, which is the opposite of what the split is for. `stay_silent` names
the choice instead, so the pair is the two things you can do with a turn - speak, or do not -
and `nothing_to_say_because` collapses to `because` now the tool name carries the rest.

`say` was left alone on the same reasoning. It undersells what it does, but it does not
misdirect, `then` already names the second half, and `converse(text, then="keep_working")`
would contradict itself. It also matches `jarvis say` and `POST /say`, and rule 1 - the one
thing no schema can enforce - leans on it being a short blunt verb.

Folding the two into one tool was considered and rejected. It needs `text` optional, which
makes "state a reason for silence" a conditional requirement, and a flat JSON Schema cannot
express that. The check would drop back into the function body, which is precisely the
enforcement being bought here.

Where the rule lives decides whether it fires. Stated only in the instructions and in
`jarvis.md`, it was ignored: both are read once, a long way back, while the choice is made
the instant a listen returns. That result carried "do the work, then call say() with
the answer" - the opposite - and the nearer text won every time. The fork now sits in the
result itself, and neither loop diagram teaches work-then-speak any more.

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

**Swapping the predicate quietly moved two other settings.** `min_speech_seconds` guards
against a click being taken for a phrase, and it used to be met by room tone alone - so at
0.3s it was no test at all. Counting real speech made it a real test, and "No." and "Stop."
were dropped without trace. It is 0.15s now, and losing "stop" is the kind of failure worth
a test of its own. Silero also needs hysteresis, which its own implementation has and this
did not: speech starts at `vad_threshold` and holds until `vad_threshold - vad_hysteresis`,
so a quiet consonant mid word is not a pause. Any predicate swapped in here has to be checked
against both, because neither is visible in the loop that uses them.

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

**Cancel SAPI from the thread that owns it, not from the caller.** `speak()` hands SAPI the
text asynchronously and polls `WaitUntilDone`, checking a flag between polls and issuing the
purge itself; `stop()` makes no COM call at all. It used to speak synchronously and cancel with
a purge from whichever thread called `interrupt()`, on the belief that a purge cuts a
synchronous `Speak` short from outside. It does not. The voice lives in the apartment that
created it, so a call from another thread has to be marshalled in, and that cannot happen while
the worker is blocked inside `Speak` - the purge is delivered only once the utterance it was
cancelling has ended, and it blocks the caller until then. Measured: `interrupt()` returned
after 5.02s on a 4s utterance, against 0.000s and a cut at 1.12s afterwards. `close()` was
paying that cost on every shutdown.

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
  next calls `stay_silent`. An MCP notification could improve that, if clients honour it
- Nothing fills the silence if the agent forgets to speak before slow work. That is
  deliberate - see above - but it does mean a forgetful agent sounds broken
