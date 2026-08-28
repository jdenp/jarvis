# JARVIS - design notes

Why things are the way they are, for whoever works on this next. `context/` holds three kinds
of file: `soul/jarvis.md` is character, `tools/tools.md` is generated from the code, and
`memories/` is everything about the desk - `navigation.md` by hand, `memories.md` by JARVIS.

## What this is

A voice assistant that owns its own loop.

```
mic thread ──▶ queue ──▶ STT ──▶ transcript ──▶ brain ──▶ tools ──▶ speech
     ▲                                                               │
     └─────────────── muted while speaking ◀─────────────────────────┘
```

The brain is `brain.py` and it always runs - a missing model stops JARVIS starting rather
than quietly leaving it as ears and hands, because listening, transcribing and answering
nobody looks exactly like working.

There was a standalone assistant here once - its own LLM, a skills registry, a persona - and
it was removed deliberately, on the argument that two things which can answer the user is one
too many. That argument was right and still is, which is why there is one brain and no switch
beside it. What changed since is the position, not the count: the model that answers is
inside the loop rather than beside it, and **the model's reply text is the speech**, so there
is no call to forget. Five mechanisms were built and removed trying to get that guarantee
from outside the loop before it was got by moving the loop.

`jarvis serve` is a daemon rather than three separate programs for one reason: **only one
process can own the microphone.** `jarvis say` from another terminal has to mute the same
microphone that is listening, or JARVIS transcribes itself. So the daemon holds the hardware
and the CLI is a loopback HTTP client.

## The interrupt is a blocking read

`GET /heard?since=N&wait=55` returns immediately if there is anything after `N`, and
otherwise blocks on a `threading.Condition` that `Transcript.add` notifies. `jarvis next`
asks once and returns the instant a sentence lands. No polling, no timer.

Ids are monotonic and survive restarts - `Transcript._resume` reads the last id out of the
JSONL rather than replaying it - so a client holding a cursor never misses or repeats an
utterance across a reconnect.

The latency floor is whatever `pause_threshold` is set to, currently 1.2s, plus about 0.2s
of Whisper. Measured cost from transcript to caller is ~0.0s, so optimising the transport is
pointless. It is set high deliberately: being cut off mid sentence is a worse experience than
waiting, and the two trade directly against each other. The ceiling is `phrase_time_limit` -
see below.

`max_wait_seconds` caps the wait a single call may ask for, so a client with a timeout of its
own gets an empty answer and asks again rather than erroring. Still not polling, just a long
poll.

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
optional, then removed. Once the model was deciding for itself what was meant for it, the
matching only ever produced false positives - and `hotwords="JARVIS"` biased Whisper's
decoder towards producing the word, so it manufactured "JARVIS" out of room noise and put
it in the transcript. Passing everything through verbatim is both simpler and more
accurate; the model is better placed to judge than a string match.

**Silence must never be the whole response.** An utterance with no wake word used to be
dropped at DEBUG, so it vanished without trace and looked identical to a hang. It logs at
INFO now, naming the word to use.

**Own the capture loop, so a phrase can end while the room is still noisy.**
`speech_recognition` ends a phrase after `pause_threshold` of *consecutive* buffers below the
energy threshold, and resets that count on any single buffer above it. One keyboard click a
second therefore holds a phrase open indefinitely, and the only thing that ends it is
`phrase_time_limit` - so a sentence spoken into a noisy room reaches the brain a minute
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

**150ms of silence in front of every utterance.** Kokoro puts about 200ms of quiet before the
first phoneme and `kokoro-onnx` trims it off, so that the batches of a long reply concatenate
without a gap in the middle. Measured across eight lines at 210wpm, the trim stops 24 to 43ms
short of the first sound - it never eats the word - but what it leaves in front of it is 25 to
50ms and nothing else. `KokoroSpeaker.speak` then opens a fresh output stream per utterance and
writes that first phoneme immediately, into an MME device reporting 93ms of output latency.
Whatever the endpoint does while it wakes up lands on the first word, and "a" and "I" are the
only words short enough to disappear into it entirely - a reply that begins "have opened
Spotify". So the lead-in goes back on. It costs 150ms per reply and it is not decoration.

**JARVIS never speaks on its own initiative.** There used to be an `Acknowledger`: a
`threading.Timer` that spoke a canned phrase when nothing had been said for a couple of
seconds, so that a slow answer did not sound like a crash. It was the one place JARVIS
decided *what* to say, and it is gone.

Having the model supply the phrase in advance works mechanically and asks the wrong thing of
a small one: composing a follow-up that lands ten seconds later, with no context to attach it
to, is a judgement to make while it should be thinking about the actual question. And a
canned timer left in alongside a model that now speaks first means two holding lines in a row
whenever it is a little slow, which is worse than either alone.

What replaced it is a rule in the prompt: if the answer is not immediate, say one line before
starting. The model knows what it is about to do, and a timer never can. If it forgets, there
is silence - and silence is the honest signal that it forgot, rather than something papered
over.

**0.8.0: own the loop, and the problem stops existing.** Speaking used to be a tool the
model had to remember to call, and everything built to make it remember was a string in a
tool result - which is advice. It was forgotten anyway, at the end of a turn, which is
exactly where a model stops remembering. The way out was not a better string.

`brain.py` holds the loop: hear, call the model with the tools attached, run them, speak the
reply. **The reply is the speech.** There is no say tool, so there is nothing to forget - the
`content` of the model's last message goes to the synthesiser, and a model that writes prose
instead of calling a tool has, by doing exactly that, answered. The failure this repo spent
five mechanisms on is not fixed; it is unrepresentable.

Two holes remained after that and both are closed in the loop rather than in a prompt. A
turn can run out of tool calls without ever writing prose, so the last call of any turn is
made with `tools` omitted from the request: with nothing to call, prose is the only thing
the model can emit. And a model can return an empty message, so an empty answer *after work
was done* is spoken as a failure - reporting that beats silence, which sounds identical to a
crash. What is deliberately still possible is staying quiet, because there is no wake word
and some of what arrives is other people. That takes a positive act now: a reply with no
letters or digits in it, which the prompt asks for as a single hyphen.

Two things came free that had looked impossible. The loop drains the transcript between
tool calls, so speech that arrives mid-task is read before the next step rather than after
the wrong thing has been done - nothing could preempt the old loop, and that was a property
of not owning it. And there is one context during voice work rather than two, which on a
single-slot llama-server is the difference between reprocessing a prompt every turn and not.

What it costs is everything an agent framework was doing for free: a system prompt, history
that does not grow, malformed tool calls, retries, and a model that loops. `Toolbox.run`
turns every one of those into a result string rather than an exception, because a tool call
with no result leaves the conversation unable to continue at all.

**The learnings are the model's to write, not ours to guess.** Almost everything that makes
this desk workable is discoverable only by getting it wrong: a window whose tree is empty
until it has been focused once, an application that reports 25 elements and no targets
because it is still building itself, which of four identically labelled Close buttons is the
one that works. Every one of those was found by watching a session fail, and every one of
them is different on the next machine.

Writing that into the system prompt by hand does not scale and does not travel. So
`memories.py` gives the model a `remember()` tool, the whole list is read back into the
prompt at the top of every turn, and a lesson written at half past two applies at half past
three. Re-read rather than cached, which also means editing the file by hand takes effect on
the next thing anyone says.

Three decisions inside that are worth keeping. It is plain markdown bullets under
`context/memories/`, so a memory that has gone wrong is fixed by deleting a line and a human
can add their own - nothing parses it beyond reading the bullets. It is capped, because it is
prompt paid for on every single call, and the cap drops the *oldest*: the desk changes, so a
lesson about an application that has since been updated is worse than no lesson. And the
only nudge to write anything down comes attached to a refusal, which is rare and is exactly
the moment there is something to learn - nudging after every call would fill the list with
notes about things that worked.

What it must not become is a diary. The tool description says so explicitly, because a model
asked to remember things will happily write down what the user likes for breakfast, and that
is context spent every turn forever on something no tool call needs.

**The tool descriptions are generated into `context/tools/tools.md`, not read from it.**
Asked where JARVIS remembers its own tools, the answer is that it does not: the schemas are
in every single request, so there is nothing to load and nothing to forget. But `context/`
having a soul and memories and no account of what the thing can actually do was a fair
complaint, and after the system prompt these descriptions are the largest influence on
behaviour - reading them should not mean reading Python.

So `jarvis tools --write` generates it and a test fails if it has drifted, exactly as
`config/defaults.json` works. The direction matters: making the file authoritative would
split each tool's prose from its signature, and prose out of date with a signature is
believed over the signature. Keeping it generated means the file is always right and is never
load-bearing.

**The web, and the promise it costs.** `search_web` and `read_page` are the first things here
that leave the machine, which is why they are a switch and why `privacy_report` names
DuckDuckGo in the startup line. They are on by default anyway, and that is a departure worth
justifying: `stt.backend = "google"` and `tts.engine = "edge"` are opt in because each
replaces something that already works locally, and there is no local version of the web. Off,
the feature does not exist rather than falling back. The line printed at every startup is what
keeps that honest, so it is not a comment on the promise, it is the mechanism.

The HTML endpoint rather than an API because every search API worth using wants a key, and
this is meant to work from `uv sync`. The trade is real - it is somebody's page rather than
somebody's contract - so the parse fails towards "no results came back, say you could not find
it" rather than towards nonsense, and `brain.search_url` takes a SearXNG instead. Adverts are
filtered on the way out: they arrive first, and a model reads the first result as the best
answer and repeats it out loud as fact.

The rate limit is one query a second and is kept rather than discovered. Waiting 300ms before
a search is invisible in a conversation; being refused costs a whole turn, and the refusal is
the nasty kind - a **202** carrying a challenge page, a success code that `raise_for_status`
and every other client is perfectly happy to wave through as an empty result. So the status is
checked explicitly, one retry covers being throttled anyway, and the message says which of the
two it was, because "slow down" and "no such thing" are different answers to say out loud.

**One soul file, and the desk is not part of it.** There were two for a while and nobody
could say where the line was - because there is not one. The character does not change with
the mechanics. So there is one `soul/jarvis.md`.

The second split is the useful one: character in `soul/`, the desk in `memories/`. How to open
an application is not part of who JARVIS is, and keeping it in the prompt is how it came to be
deleted during a rewrite for looking tool-adjacent - after which a session decided Teams was
not installed and went shopping in the Microsoft Store. Every markdown file under `memories/`
is read, at any depth, so a new one is a new file rather than a code change.

The same distinction repeats one level down, in `memories/navigation/`. `os-navigation.md`
ships and is edited by hand: how Windows behaves, true on any machine, and therefore worth
committing. `user-navigation.md` is JARVIS's own and is gitignored, because it is about one
desk. Promoting a line from the second to the first is a text edit, which is the whole reason
they are separate files rather than one with a convention inside it.

Only the two files JARVIS writes are capped. The rest are bounded by whoever wrote them, and
trimming the curated half to make room for the accumulated half would be the wrong way round.

**The prompt is a file, and shorter is better.** It lives in `context/soul/brain.md` rather
than as a string in `brain.py`, because it is prose: it is tuned by reading it out loud and
changing a word, not by editing Python, and it is the largest single influence on behaviour in
the repository. There is no copy in the code to drift from, and a missing file stops the brain
with the filename rather than substituting a stand-in personality nobody asked for. The
microphone paragraph sits inline behind `<!-- ears -->` markers, so whoever opens the file
reads the whole thing instead of a prompt with a hole in it.

It was cut in half on the same argument that shrank the accessibility tree: the more
instructions it carries the more carelessly each one is followed. What went was everything the
tool schemas already say - `expecting` is explained in `click`'s own description, and the
"nothing clickable, use the keyboard" advice arrives in the scan result at exactly the moment
it applies. What stayed is what only the prompt can say: that words end the turn, that silence
is a hyphen, and how to sound.

**The last word of a turn needs the same guard as every other.** With the step budget gone
after eight calls, a model carries on in the shape it has been writing in - so the answer that
came back from the tools-removed call was `<tool_call> <function=look_at_screen> </function>`,
and it went through the speakers with the tags in it. Removing the tools from the request does
not stop a model typing one out; only checking what it said does. It now gets one chance to
say it in English and then the failure is reported out loud, because silence after eight tool
calls is worse than admitting it could not manage.

**It writes its own notes, after the answer has gone out.** A lesson only outlives the
conversation it was learned in if somebody writes it down, and asking a model to remember
mid-turn competes with the thing it is actually doing. So a turn that used its hands is
followed by one more call: look back, and is there anything about how this desktop behaves
that would have saved a step? It runs after `_speak`, and speech is queued and played on
another thread, so it happens in time nobody is waiting through.

Three things make it work rather than fill the file with rubbish. It is told that most turns
teach nothing and that replying with nothing is the expected answer. It gets at most two lines
and they must be bullets. And crucially it runs with reasoning OFF and a 200 token leash -
left thinking, it spent four thousand characters weighing up whether one line was worth
keeping and then ran out of room before writing it. Reasoning earns its cost when a tool has
to be chosen; this call has no tools.

It never touches the conversation: the question is asked over a copy of the history and the
answer is thrown away. What it writes goes to `user-navigation.md`, not to the file
`remember()` uses, because a note about how to minimise a window is not the same kind of thing
as a note about this machine.

**Reasoning and the answer share one budget, which is how a turn ends in silence.** The worst
failure so far had no symptom at all: `brain.max_tokens` was 600, sized for a forty word
reply, and a hard think produced 2473 characters of reasoning that stopped mid sentence with
no answer written. What reached the speakers was "Sorry sir, I could not put an answer
together", and nothing in the log said why, because nothing read `finish_reason`.

Three changes, and the order matters. It is read now, and a truncated generation logs a
warning naming the cap - that alone turns an unexplainable failure into an obvious one. The
cap went to 2000, because it is a ceiling rather than a reservation and being tight buys
nothing. And an answer that is empty *because* it ran out of room is asked again with twice
the room, which is the only recovery that makes sense: asking again inside the same cap gets
the same nothing.

A reply cut off after a complete sentence is still that sentence. Truncation only matters when
it left nothing to say.

**Anything with a text answer is a shell question.** Asked to find a file on the desktop, it
opened File Explorer and clicked: Downloads, scroll, a refusal, This PC, a COM error, several
minutes. `Get-ChildItem` answers it in one call, on folders nobody has open, with exact names
and nothing redrawing underneath. The pointer is for applications that only exist as a window,
and that distinction now lives in both places it can be read - `os-navigation.md` and
`run_command`'s own description. Tried afterwards, the same request became two commands and an
answer.

The same transcript threw a `COMError` out of `FindAllBuildCache`: "an event was unable to
invoke any of the subscribers", which is what a window redrawing mid enumeration looks like.
It succeeded immediately on the retry the model made itself. It is retried once inside
`uia.elements` now, because spending a step and showing a model a stack trace is a lot to pay
for a moment of bad timing.

**The second identical refusal has to say something else.** "Look again and use the new
numbers" is right the first time and useless the second, because looking again gives back the
same numbers: a session clicked `System` in a terminal, looked again, clicked `System` again,
and spent the rest of its budget going round. `Toolbox` remembers the last refusal, clears it
on anything that works, and on a word for word repeat says to stop clicking, that the keyboard
reaches what the pointer cannot, and that a shell command is usually the shorter way round
anything to do with a program running or not running. An earlier version had this and it was
lost in a port; a failure mode that has been solved once should not have to be found twice.

**Talking over it works because the reply is read a token at a time.** The stream is what
makes it possible at all: `Model._streamed` checks the room every 300ms, and anything heard
raises `Interrupted` out of the middle of the read. Abandoning the request is what stops the
server generating too, so it costs nothing to change your mind. Everything found so far is
kept - "no, the other one" should build on the look that already happened rather than start
the turn from nothing - and only calls carrying tools are interruptible, because the last call
of a turn is one sentence from being spoken and losing it would throw away the work.

Two smaller decisions inside that. The check is every 300ms rather than every token, because
at fifty tokens a second that would be fifty transcript reads to shorten a delay nobody can
perceive. And the interruption is not drawn on screen by the loop - the service already drew
it when it was transcribed, and twice on screen reads as having been said twice.

**An interruption gives the step budget back.** Steering worked exactly as built and the turn
still failed: eleven of twelve steps went on opening the wrong thing, "no, go in the taskbar"
arrived and was understood, and there was one step left to do it in. The budget is meant to
stop a model going round in circles, and a person changing their mind is the opposite of that
- it is the best evidence available that the next steps are worth taking. So the counter goes
back to zero, and there is a first step again, which also means a lead-in is worth saying
again. The only thing that can spend the budget this way is somebody choosing to keep talking.

**Cutting the speech off is a second interruption, and a later one.** The one above happens
mid turn, while the model is still working, and it is quick because the stream is checked every
300ms. Once the reply is being read out there is no stream left to check: what arrives is a
finished phrase, and a phrase does not exist until somebody has stopped talking for
`pause_threshold`. So `service._stop_talking` lands a couple of seconds after they began rather
than on the first syllable. It buys the rest of a long wrong answer, which is worth having, and
it is not barge-in on speech onset. Silero would give that, at about 0.3s, but it is
volume-blind by design - the point of it - so bleed from the headphones scores as speech and
JARVIS would cut itself off mid word.

It is gated on `audio.listen_while_speaking`, because with the microphone shut through a reply
a phrase landing now was recorded before that reply started and is nobody talking over
anything. Typing is not gated: nothing typed can be an echo and nothing typed arrives late.

**Tool results go in the log, not only on screen.** `run_command("start teams")` returned "the
system cannot find the file", the model concluded Teams was not installed, and the log recorded
only the command. Half a post mortem is no post mortem: the first line of every result is
logged now.

**A long reply outlives the memory of having said it.** JARVIS answered a search with 379
characters, heard itself thirty seconds later, and replied "That's right. Is there anything
else I can help you with?" The matching was never the problem - fed both strings it recognises
them perfectly. The window was: `MEMORY_SECONDS` was a flat 20 seconds, and a phrase is only
transcribed once it *ends*, so a thirty second reply arrives thirty seconds after it was
remembered. The memory now lasts as long as the speech plus the window, which is the only
version of this that is right for both a two word acknowledgement and a paragraph.

**Being refused is worth remembering.** The search engine cuts you off for minutes, not
seconds, and every further request extends it - so a live session spent eight attempts and
forty seconds rediscovering the same refusal. `_blocked_until` is module level, the next
search does not go out at all, and the result says how long is left and offers the route that
did work, which was opening a browser and searching there.

**Nothing shortens the reasoning except asking it to.** Measured against the real endpoint:
temperature does nothing to thinking length (359 to 545 characters across 0.0 to 0.7, no
trend), and `reasoning_budget` and `thinking_budget` are ignored by this build. Fewer tools
does not help either - with none at all it thought the longest of the lot. One paragraph in the
prompt, telling it that a greeting is not worth deliberating over, took the median from 525
characters to 182 and the completion from 150 tokens to 64, with the tool calls intact. That
is the whole dial, and it is the cheapest one in the file.

**What everything costs, measured.** Against the real tokeniser, on a 98,304 window:

| | tokens |
| --- | --- |
| system prompt | 622 |
| twelve tool schemas | 2,042 |
| **every request starts at** | **2,664** |
| one target in a scan | 15 |
| a full 200 target scan | 3,057 |
| a page at `page_chars` | ~500 per 1000 characters |

Two things fall out of that. The base is under 3% of the window, so almost every cap in
`BrainConfig` was set cautiously against a budget that turned out not to exist -
`page_chars` and `shell_output_chars` were cutting answers in half to save a thousand tokens
of ninety-five thousand spare. And the only figure that is charged on *every single request*
is `max_memory_chars`, which makes it the one to be careful with; the rest are paid once and
then carried as history.

`max_targets` is the exception that stays where it is. Two hundred targets is 3k tokens and
affordable, but the argument for the cap was never the tokens - it is that a model chooses
worse from a longer list, which is the whole accessibility tree lesson. Affording more is not
a reason to offer more.

**Forgetting happens twice, and the cheap half runs first.** Past
`brain.squash_fraction` of the window - 0.65, about 64k of a 98k one - `_squash` walks the
tool results oldest first and replaces the text of each with a line naming the tool that ran.
It stops as soon as the estimate is back under. The last two turns are never touched, because
the last scan is what "no, the one below it" refers to, and neither is anything under four
hundred characters, since a short result is usually a fact worth having and squashing forty
tokens to thirty saves nothing.

That works because of what a long session is actually made of. A crowded window scans as three
thousand tokens of numbered targets which were stale the moment anything was clicked, and an
afternoon of them is most of the window spent on lists nobody will read again. What was asked,
what was run and what was said is a few hundred tokens a turn and reads as a memory of the
afternoon. The call stays with its id, so nothing is orphaned and the endpoint still sees a
well formed conversation - which is exactly why this is safe and deleting the message would
not be.

**`_trim` is the other half, and it is not compaction either.** It keeps the system prompt and
the last `brain.history_turns` turns whole and deletes everything before them. Cutting at a turn
boundary is the load-bearing part, because half a turn leaves a tool result whose call is gone
and some endpoints reject that outright. Nothing is summarised: ask about something from ten
turns ago and it is gone without trace.

Turns are not the same size, which the turn count quietly assumed: a greeting is fifty tokens
and a turn that scans a crowded window twice is six thousand, so twenty of the second kind
would overflow a 98k window - and an overflowing request fails outright rather than degrading.
So there are two limits and whichever bites first wins. The measured prompt size is the
backstop, dropping one turn per turn taken, which is enough because the conversation only
grows one turn at a time.

That was chosen on the grounds that voice conversations are short, and it is still mostly
true, but it is dropping rather than compacting and the meter in the corner now makes it
visible. Squashing is what got built instead of summarising, and it is worth being clear about
what it is: not a compaction, just a cheaper thing to throw away first. It costs no model call
and no waiting, and by the time it has run a few times the turn count is usually enough on its
own. It also made the old default look silly: the prompt sits at 2.6k of a 98k window, so
six turns was throwing conversation away to save nothing, and 20 is the number now. Trimming
is the one thing that invalidates a cached prefix - everything after the system prompt shifts
- so trimming rarely is faster as well as more useful. Every real alternative costs something a voice loop can feel. Summarising on eviction
means a model call between turns, at the moment somebody is waiting. A rolling summary means
the summary is in every request forever, growing, and being rewritten by a model that will
eventually rewrite it wrongly. It is in the README's "not done yet" rather than half built.

**A lock key is watched, not hooked.** `keyboard`'s low level hook lives in this process, and
Windows does not deliver input to an unelevated process while an elevated window has the
foreground. Task Manager, an admin terminal, regedit: presses there were dropped silently, and
because the old code counted presses rather than reading state, one dropped press inverted the
key for the rest of the session - the lamp said one thing and JARVIS believed the other.

Num Lock, Caps Lock and Scroll Lock each have a state Windows keeps for itself, and
`GetKeyState` reads it from any thread with nothing to pump. So a press is a lamp that differs
from the lamp before it, read eight times a second. It cannot be denied, it cannot drift, and
it drops the `keyboard` dependency for the default configuration. Anything without a lamp still
hooks, and that is the only thing the extra is for now.

**The same elevation wall is why Task Manager cannot be clicked.** An unelevated process is
shown one element and no targets, forever, and its clicks and keystrokes go nowhere with no
error. A live session read that as a window still drawing itself and spent four minutes
waiting, focusing, scrolling, screenshotting and pressing alt+f4, then wrote four memories
blaming its render time. So the scan asks: `runs_as_admin` on a window offering nothing, and
the answer says which of the two it is. Being refused the process handle counts as elevated,
because that refusal is the same news.

**Chat mode is a front end, not a second implementation.** `jarvis chat` is the same
`Brain`, the same `Toolbox` and the same memories, with `ConsoleVoice` in place of
`ServiceVoice` - two methods, `hear(timeout)` and `say(text)`, and `run_forever` cannot tell
them apart. A test compares the two signatures so that adding an argument to one breaks
loudly rather than only at runtime in the other.

It earns its place twice. Over SSH there is no audio device, so a keyboard is the only way
in. And a voice session is a terrible place to debug a model: the tool calls are invisible,
there is nothing to scroll back through, and every experiment costs a sentence read out loud
at conversational pace. Chat mode prints each call as it goes, which is how most of the
prompt wording in `brain.py` got settled.

`hear(0.0)` - the loop's mid-task check for somebody talking over the work - returns nothing
here rather than reading stdin. One line is read at a time in a chat, so barge-in is
genuinely absent rather than faked, and pretending otherwise would mean a half-typed line
being snatched mid-task.

**One terminal, and no dependency for it.** `ui.py` draws both front ends, because there is
one conversation whether it arrived by microphone or keyboard, and two renderers would drift.
Permanent lines scroll and a single live line under them says what is happening now, redrawn
in place and erased before anything permanent is written - that last part is the whole trick,
and getting it wrong splices a half-drawn status into the middle of the conversation.

`rich` and `textual` would both do more. Neither is worth an install for a status line and
five colours, and an alternate-screen application would be actively wrong here: the point is
a scrolling record you can pipe, redirect and scroll back through, which is also why
everything degrades to plain lines the moment the output is not a terminal.

**Typing goes in where speech does, and nowhere else.** `service.typed()` puts the line
straight into the transcript, so the brain, `heard.jsonl` and the `you >` on screen all see
the same thing and none of them can tell the difference. It skips exactly two
of the things speech goes through: the echo guard, since nothing typed can be JARVIS hearing
itself, and the pause, since shutting the microphone is not a reason to ignore somebody who
has chosen to type.

It is read a character at a time rather than with `input()`, and that is the whole reason
`typed.py` exists. `input()` owns the terminal from the moment it is called, and the live line
under the conversation wants the same row - so `Ui.hold()` puts the status away, the line is
echoed by hand, and `release()` brings it back. Nothing is drawn until a key has actually been
pressed: `msvcrt.kbhit()` is a peek at the console buffer every fiftieth of a second, and a
prompt sitting there unanswered would be clutter that means nothing most of the time. Escape
abandons the line, which is the way out for whoever pressed a key by accident - and pressing
one by accident is precisely why it waits for a keypress rather than showing a prompt.

**The live line shows the model thinking, and then it does not.** Streaming exists here for
exactly one reason: a spinner says a model is busy, and the last line of what it is actually
reasoning about says whether it is busy on the right thing. It is set without redrawing -
tokens arrive far faster than anybody can read them, so the animation thread picks the text up
at its own pace and the terminal is written to eight times a second instead of hundreds.
Nothing keeps it. That is the whole of "collapsed": shown while it happens, gone when there is
an answer, and never in the scrollback.

Everything is reassembled into the same `Reply` a single response would have produced, tool
calls included - keyed by the delta's `index`, because a model can start a second call before
finishing the first and the arguments arrive a few characters at a time. A half-read stream is
a failure rather than a short answer. `brain.stream = false` for an endpoint whose SSE cannot
be trusted; nothing else depends on it, and with no terminal to draw on it does not stream at
all.

**One throwaway request at startup.** The system prompt and the tool schemas are most of every
request and never change, so a server that reuses a cached prefix processes them once. Doing
that at startup spends a second or two of nobody's time instead of putting it on the first
answer, which is the one that would otherwise feel broken. A one token limit, and it never
enters the history: that conversation did not happen.

**Speaking ends the turn, so nothing may be announced in advance.** Two tool descriptions said
"say what you are about to do before you do it", carried over from a design where a lead-in
and the work were separate calls. In this loop prose *is* the end of the turn, so that
instruction is not merely unhelpful, it is impossible to follow - and it was followed exactly.
Asked to stop listening, the model replied "Pausing transcription, sir. Just call my name to
start me up again", called nothing, and went back to listening. It also invented a wake word.

The fix is the ordering, everywhere: do it, then say what happened, or write the sentence in
the same message as the call. The system prompt now says so in as many words, because it is
the one rule of this loop that nothing else has - anywhere else, saying you are about to do
something is normal and harmless.

**Reasoning off is fast and worse at choosing.** `brain.thinking = false` sends
`chat_template_kwargs: {"enable_thinking": false}`, and it is a genuine improvement on latency:
a greeting comes back in 0.4s against 2.2s. Probed against one simple tool it still emits a
proper call. With all ten in front of it, it began writing calls as prose -
`search_web(query="weather in Melbourne today")` in the content, which on this path is read
out loud. That is the same lesson as the accessibility tree in a different costume: the model
is fine at choosing between few things and degrades across many.

So thinking stays on, and the *failure* is guarded rather than the setting forbidden. A reply
that is entirely a call to a tool that exists is caught before it reaches the speakers and
sent back with a note that it did not run. It costs one round trip and turns the worst
possible output into a retry, which also makes `thinking = false` safe to choose.

**A status line must not lie.** The first version said "listening" unconditionally, which was
wrong the moment the microphone was shut - by the hotkey or by the model - and made a working
Num Lock look like a dead one. It asks the voice what it is waiting for now. The same
regression had a second half: pausing only logged at INFO, and INFO stopped reaching the
terminal when the UI took it over, so the key worked and said nothing. Pausing draws a line
of its own now. Both were the same mistake, which is that moving output to a new place is not
free - anything that only reported through the old one goes quiet.

The other half of it is that logging had to stop competing for the same screen. A stream
handler writes straight to stdout and walks over the live line, so once the brain is running,
`_hand_the_terminal_over` swaps that handler for `LogToUi` - warnings appear as part of the
conversation, and everything at INFO stays in the file where the detail belongs. The boot
lines still go through plain logging, because a five second Whisper load with nothing on
screen looks like a hang.

**The accessibility tree is not a prompt.** Handing the whole thing to a model does not
work and the numbers say why: one Teams window is 810 nodes, Outlook in a browser 833,
and in both cases the great majority are panes, groups, static text, images and
scrollbars. A model given the lot picks something plausible and wrong. Filtered to what
can actually be acted on, the same windows are 54 and 142. `screen.py` does the filtering
and `uia.py` does the COM, which is the split that lets the judgement be tested without a
desktop - every decision in `select()` runs against synthetic `Element`s.

**The indirection is the whole trick, not the picture.** Set-of-mark pipelines usually
draw numbers onto a screenshot and let a vision model read them back. The part that earns
its keep is not the drawing: it is that the model names an id and the host owns the
coordinate. Doing it in text costs nothing, works with no vision model loaded, and the
signature makes the wrong answer unrepresentable - there is no x or y argument on any of
these tools to get wrong. The marked screenshot is still drawn on request, for whoever is
debugging a misclick.

**Separate tools rather than one action verb.** The obvious shape is one `act(action,
target, text)` call. It is worse here, because the schema can then only say "text is
sometimes required" - where `click(target, expecting)` and `type_text(target, expecting,
text, then)` each state exactly what their own call needs, and a missing argument is
rejected before the tool body runs.

**`expecting` is checked.** A target number is worthless on its own: a number from a scan taken before the list scrolled still resolves
to a perfectly good coordinate, and that is how automation presses delete on the wrong
row. So clicking requires the label as well, and refuses if the two disagree. The check
is deliberately loose - either string containing the other, case and whitespace ignored -
because labels are truncated before they reach the prompt and a model shortens a long one
further. Strict equality would refuse correct calls; the mistake being caught is not
subtle.

**Verify the point, not the runtime id.** Before a click, whatever is under the target's
centre is compared against the target. A runtime id settles it when it matches, but
runtime ids do not survive a control being rebuilt, which virtualised lists do
constantly - so the same name and role in the same place is accepted too. Two things fall
out of this for free: occlusion (a desktop icon behind a terminal is genuinely not
clickable, and the refusal says so rather than clicking the terminal) and layout drift.

**Raise the window before checking, not after.** The first version activated the window
after resolving the target, which meant a background target failed the point check every
time - what is under a point in a covered window is the window covering it. Input goes to
the foreground regardless of what was scanned, so raising has to come first. The activate
is skipped when the window is already in front, which is the common case and keeps a
click free of the settle delay.

**A minimised window is refused rather than scanned.** This one was a surprise. UI
Automation happily returns a full tree for a minimised window, with plausible looking
coordinates left over from wherever it was last drawn, so acting on them presses things
in whatever application is actually there. `GetWindowRect` is the honest witness - it
answers -32000 - and `IsIconic` is the tidy way to ask.

**Numbers are not invalidated after an action, only distrusted.** Tempting to throw the
scan away every time something is pressed, since anything pressed redraws something. It
is the wrong trade: ticking three checkboxes would cost three rescans at 0.2s each, and
the point check already turns a stale number into a refusal rather than a misfire. The
result says to look again instead, at the point where the next tool is being chosen.

**Truncation must not amputate a region.** The first version took the first N targets in
reading order, with N at 60. Reading order runs top to bottom, so the tail it discarded was
the bottom of the window - and a media player keeps its transport controls there. A live
session asked JARVIS to press play in Spotify: 166 real targets became the top 60, the play
button was in the 106 dropped, and the result said only `not_shown: 106`. The request was
not hard, it was impossible, and nothing in the result said so.

Two changes. The cap is 200, which no normal application reaches - Spotify 166, Outlook in
a browser 177 - so truncation stops happening at all in practice, and 200 targets is around
4k tokens, which the context can afford. And when it does happen the cut is an even
spread rather than a prefix, so every region of the window is represented and the result
says it is a sample. A sample degrades; a prefix hides a third of the screen completely.

**Say when looking again will change nothing.** The same session scanned Spotify four times
identically and the taskbar five times, each result indistinguishable from the last, with
nothing to say so. The scan is now fingerprinted by window and target labels, and a repeat
is flagged `unchanged` with the two things that would actually alter it - focus_window, or
scroll. Cheap, and it is the difference between a loop and a decision.

**Name the tool, not the remedy.** The minimised refusal said "bring it to the front
first". The same session hit it four times running and never called `focus_window`, because
the message described the fix in prose and never said which tool performed it. It names the
tool now, and says that retrying the same call will refuse again. The general lesson is the
one that fixed the voice loop's result too: a refusal has to end on the call to make next.

**Keep the test suite out of the real log.** `cli.main()` configures logging, so any test
going through it attached a rotating file handler to the repository's own `logs/jarvis.log`
- and from then on every warning any test provoked was written there. Diagnosing the live
session above meant reading past "Pillow is not installed", "Unknown key 'nope'" and a
dozen dropped-phrase warnings, none of which had happened to the user. Measured at ~3.5KB
of noise per run, now zero.

**Check the point before raising the window, not after.** The order has now been wrong in
both directions. First it checked and then raised, so a background target failed on the
grounds of being covered by whatever covered it. Then it raised and then checked - which
broke the taskbar, because the taskbar is always on top and `SetForegroundWindow` refuses
it: the attempt logged "could not bring Taskbar to the front" and the check that followed
refused a click on a button that had been perfectly clickable a moment before.

Both are fixed by checking first and treating failure as the trigger to raise, rather than
raising unconditionally. A target already under the pointer needs nothing done to it, which
is the common case and now also the cheap one - no activate, no settle delay. Only when the
point says the target is not visible does the window get brought forward, and then it is
checked again. The taskbar passes on the first check and is never touched.

**Not every window has a tree, and the Start menu is one of them.** Asked to open Spotify
and play, it pressed the Windows key, scanned what came up, and got one element: a
target labelled "Search box" whose rectangle was the entire Start panel. Its centre is
therefore not a control, so the point check refused three type_text calls in a row - each
time correctly, each time with the same words, and each time it rescanned and got
the same single target back. It got there in the end by pressing playpause, and reported
that as having opened Spotify.

Three separate things were wrong, and only the first is about the Start menu.

`type_text` required a target. After the Windows key the search box already has keyboard
focus and there is nothing to click, so "type where the caret is" was not expressible.
`target` and `expecting` are optional now: name one and it is clicked and checked exactly
as before, leave them out and the text goes to the focus. Naming a target without
`expecting` is refused in the tool body, since a JSON schema cannot make one argument
conditional on another and silently dropping the check would be worse than either.

A single target covering its whole window is reported rather than offered. It means the
accessibility tree never populated - a UWP surface or an Electron app that has not
activated it - and no amount of scanning will produce a control. The scan comes back
`nothing_clickable` and points at the keyboard.

And a refusal repeated verbatim is useless. The message said to look again and use the new
numbers; the new numbers were the same numbers. The second identical refusal now says so
and names the two things that are not clicking.

The general shape, which is the third time it has come up in this file: a refusal has to
end on something different from what the caller just tried.

**Placing repeated labels.** A browser offers four buttons called Close and a coarse
position does not separate them - a tab strip is all in the same ninth of the window.
What does separate them is the thing before them in reading order, which is how anyone
reads a tab strip: `Close, after "Gutenberg / Alpha - GitLab"`. Only repeats are placed;
on a unique label it is noise. The placing runs before the `matching` filter, or a search
for "close" strips out every neighbour it needed.

**Looking is free, and acting is on by default - which it was not at first.** The split
still holds: `look_at_screen` and `screenshot` read and touch nothing, so they are always
registered, while everything that moves the pointer sits behind `screen.control`. What
changed is the default.

Off was the obvious choice and it was wrong. Three reasons, in ascending order of how much
they cost. The point of the feature is to act, so the interesting half was disabled out of
the box. The model cannot discover the flag on its own; the best it can do is relay a message
asking the user to set it, which it did, correctly, and which nobody read. And the failure
mode is indistinguishable from the feature being broken - a live session spent four calls
refusing to touch a minimised window while `focus_window`, the tool that would have restored
it, was not registered at all. The diagnosis of that session initially blamed the refusal's
wording, and the wording was only half of it.

The flag stays, and the off path stays tested, because a read-only mode is a genuinely
useful thing to be able to ask for. It is just no longer what you get without asking.

**Real input rather than UI Automation patterns.** `Invoke()` is tidier where a control
supports it, but coverage is patchy - much of what a browser or an Electron app draws has
no pattern at all - and a control that quietly does nothing is worse than one behaving
exactly as it does under a real hand. Two consequences to live with: Windows silently
refuses input from an unelevated process to an elevated window, and the pointer visibly
moves.

**One automation object per thread.** COM apartments do not share objects and a tool may
be called from a different thread than the last. Rather than marshal,
`_automation()` keeps a thread-local - a few milliseconds once per thread - and
everything handed out of `uia.py` is plain data that crosses threads freely. That is the
same flattening that makes the module testable, arrived at for a different reason.

**Cache request, or it is not a scan but a stall.** Every property comes back in one
cross-process call through `FindAllBuildCache`. 810 elements cost 0.17s that way against
several seconds read one attribute at a time. The cache request has its own `TreeScope`
and it must be `Element`; setting it to `Descendants` is rejected with "the parameter is
incorrect", which reads like the scope argument on `FindAll` and is not.

**DPI awareness, set before anything else.** Windows reports rectangles in virtual pixels
to a process that has not declared it understands scaling, so on a 150% display a click
lands two thirds of the way to where it was aimed. It is set on first use of the backend,
and failing means it was already set, which is the outcome wanted anyway.

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
- Speaker identification, so a room with two people in it does not confuse it
- Acoustic echo cancellation, and it is now the biggest thing left.
  `audio.listen_while_speaking` had to go back off: with no AEC an open microphone on
  speakers transcribes JARVIS, and it answered its own weather forecast once. The text
  comparison in `echo.py` is the only defence and a long reply beat it. Headphones make it
  free and it can be turned on there. Real AEC on the capture path is what makes cutting a
  reply off work in a room with speakers in it - note that talking over the *thinking* works
  either way, since the microphone is only shut while a reply is actually being spoken
- Cutting the speech off on speech onset rather than on a finished phrase, which would take
  it from a couple of seconds down to about 0.3s. Needs a predicate that is not volume-blind,
  or it cuts itself off on its own bleed
- Nothing fills the silence if the model forgets to speak before slow work. That is
  deliberate - see above - but it does mean a forgetful turn sounds broken
- Screen control has no undo and no dry run. `expecting` catches the wrong target but
  nothing catches the right target with the wrong intent
- Nothing reads text out of a control. It can see that an edit box exists and type
  into it, but not what is already in it, so "read me the last message" is out of reach
- The scan is per window. Anything spanning two windows, or a dialog opening over the one
  being scanned, needs a second look to notice
- An application with no usable accessibility tree - a game, a canvas, a remote desktop
  window - is invisible to all of this, and a picture does not rescue it: with no targets
  there is nothing to number. That case is what a vision model detecting
  controls in pixels is actually for, and it is not built here
