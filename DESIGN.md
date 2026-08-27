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
once - `converse()` - and it returns the instant a sentence
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
a `hold=...` argument works mechanically but asks the wrong thing of a small model:
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
Instructions could not reach it. `jarvis.md`, the server instructions and the tool result
all said "always go straight back to listening", two of them in capitals, and it
happened anyway - because it is a thing to remember at the end of a turn, and the end of a
turn is exactly where a model stops remembering.

So the tool does it instead. It takes a required `then`: `then="listen"` speaks and then
performs the blocking read itself, returning the utterance in the same result, and
`then="keep_working"` speaks and returns at once for the lead-in. Answering *is* listening,
so there is no second call to forget. The argument has no default, so the schema rejects a
call that omits it - the model is made to state which of the two it is doing, and the fork
is one the lead-in rule already demanded, so it costs no judgement that was not already owed.

**Two tools became one, after two live sessions said so.** The first version kept a split:
`say(text, then=...)` to speak, `stay_silent(because=...)` to listen without speaking, with
`because` an enum whose `already_spoke_my_reply` value the server could check against what
had actually gone through the speakers. That much worked, and the checkable claim survives
today in a different form.

What did not work was the split itself. Twice, in a fresh session, the agent called the
listening tool, got "Hey Jarvis" back, wrote "Hey there! What's up?" into its own reply text
and ended its turn. Nothing was spoken either time. The lesson is narrow and worth stating
plainly: **a required argument constrains a call that happens; it cannot cause a call to
happen.** Everything downstream of "the model decided to use the tool" was already closed,
and the failure was upstream of it.

Diagnosing the second one turned up two contributing causes and one that was neither.

The one that was neither: a copy of `jarvis.md` in the client's rules directory, three days
stale, still naming `wait_for_speech` and showing a bare `say(answer)`. Fixing it did not
fix the failure, which is how the split was ruled out rather than the prose. It is written
up separately below because it is a real trap regardless.

The two that were: the result. For a two word greeting the model received fourteen lines, of
which eight were a `detail` block repeating the text with an id and a timestamp that nothing
consumed, sitting between the words and the instruction. And the instruction opened with a
question - "Can you answer right now, from what you know?" - which a model answers in prose,
because answering questions in prose is the single strongest thing it knows how to do. It
then closed on the name of the tool that does not speak. This repo had already learned that
a small model picks whichever clause it read last; the clause it read last was wrong.

**So: one tool, and the result is an instruction and nothing else.** `converse(say, then)`
speaks and then listens. `say=""` listens without speaking. Both arguments required. The
result is `heard` plus one imperative that opens and closes on `EMIT converse()`, six lines
instead of fourteen.

The argument for consolidating is not that it enforces anything - it does not, and nothing
in MCP can. It is that the second call is now *identical in shape to the first*. Under the
split, a model that had just listened had to notice it needed a different tool with
different arguments, at the end of a turn, which is exactly where a model stops noticing
things. Now there is one tool, its only interesting argument is the thing to say, and
and entering voice mode is `converse(say="", then="listen")`, the same call with nothing
to say.

An earlier version had the entry speak - `say="Yes sir?"` - on the grounds that it
establishes the pattern from call one, so the first *reply* is not also the first time the
tool is used with content. That was reverted on request, and it costs less than it would
have: `force_a_reply` below now catches exactly that transition, which is the work the
greeting was doing. Entering voice mode is silent again, which is what someone who has just
asked for it expects.

**The checkable claim survives, and got broader.** `say=""` while a reply is owed is the
claim to have answered, and it is refusable on the same evidence as before: `unanswered`
holds the last utterance heard and is cleared only by actually speaking. It is better than
the enum it replaces, because it is the behaviour itself rather than a self-report - there
is no unfalsifiable value to pick instead.

It bounces once rather than refusing outright. Returning immediately without listening,
naming what went unanswered, and clearing the debt on the way out, so the next call goes
through whichever way the agent decides. That is the whole difference from the version this
repo tried first, which blocked until something was spoken and deadlocked against an agent
that had correctly kept quiet. One cheap round trip is a cost worth paying; a hang is not.

It now chases anything heard, not only what parses as a question. `looks_like_a_question`
guarded the old bounce, and "Hey Jarvis" is not a question - it is precisely what went
unanswered, twice. The cost is that a room with a television in it pays one bounce per
utterance, which is why the message offers both ways out in the same breath rather than
insisting on an answer: answering what nobody asked is the worse failure of the two.

**Three attempts to force a spoken reply, all removed.** Worth writing down because each
looked reasonable and each failed the same way, and the next idea should have to clear a
higher bar than these did.

The first handed speech back as `isError: true` on the result that delivered it, on the
grounds that a client which will end a turn on a result will not end it on an error. That
much was true - the turn did not end, and the agent went and did the work. It never spoke,
because by the time the work was finished the error was eleven results back in the context.
It also meant lying about a call that had succeeded, on every turn of every conversation.

The second attached the outstanding reply to every screen result while it was owed. A note
on everything is wallpaper: it stops being read, and it fires on tasks that were never
going to be slow.

The third made that note one-shot and gated it on twelve seconds of silence, so an agent
that answered promptly never saw it. Better, and still a note - which is to say still
something the model can decline to act on, which is the entire problem.

The fourth, an action budget, was written and never committed: three clicks between spoken
lines, then the acting tools refuse until something goes through the speakers. That one is
genuinely unignorable, because ignoring it means the task stops progressing. It was pulled
with the others on the same judgement - a wall the agent hits mid-task is a worse experience
than the silence it prevents, and none of the four produce the thing actually wanted, which
is a closing report.

What survives from all of it is the one refusal that is not a nag: `say=""` while a reply is
owed is a claim to have answered, the server knows whether anything went through the
speakers, and a false claim is refused. That is a lie being caught rather than a memory
being prompted, which is why it keeps working.

**What worked: stop asking the agent and read what it wrote.** Every attempt above put
words into a tool result, and a tool result is advice. The thing they were all reaching for
was already on disk. Cline writes its whole conversation out as it goes - assistant
messages, thinking, tool calls - so a reply typed instead of spoken is sitting in
`~/.cline/data/sessions/<id>/<id>.messages.json`. `overhear.py` watches for new assistant
prose while a reply is owed and speaks it.

It asks the agent for nothing, which is the entire point. No schema argument to comply
with, no note to read, no protocol feature, no capability to advertise. It was checked
retroactively against every failure in this repo's history and recovers all of them,
including the two lines from the Spotify session that were written and thrown away - a
lead-in and a closing report, both perfectly sayable.

Three things it does not do, deliberately. It does not read thinking, which is verbose,
internal and frequently about the user rather than to them. It does not read prose written
for the eye - code fences, tables, headings, numbered lists, anything past
`overhear_max_chars` - because reading markdown aloud is worse than silence. And it strips
emphasis and emoji from what survives, since SAPI pronounces `**947**` as "asterisk
asterisk nine four seven".

**Bound to Cline on purpose.** The directory layout is Cline's and so is the envelope -
`origin.source`, an `agent` name, its own version string - so `looks_like_cline` checks for
it and a transcript without one is left alone and logged once. The temptation is to parse
anything with a `messages` array, and the failure mode of doing that is reading a stranger's
file out loud. `service.cline_sessions` moves the path for a portable install; it does not
make another client work.

Which is not to say it cannot be adapted. The content inside `messages` is the ordinary
Anthropic API shape, parts typed `text`, `thinking` and `tool_use`, so any client storing
raw API messages needs a reader rather than a redesign, and the seam is `looks_like_cline`
plus `transcripts`. The one thing no adapter can fix is a client that never writes the
conversation down: there has to be a transcript to overhear.

It is jank and the switch admits it. `service.overhear` turns it off, because this depends
on someone else's on-disk format and that format can move without warning - when it does,
this goes quiet rather than failing loudly. The session is picked by modification time,
which is a guess: the MCP server is told nothing about which session spawned it, so two
conversations at once means it may speak the wrong one's answer.

**It delivers the reply; it does not resume the conversation.** Worth being exact, because
the two are easy to conflate. Overheard speech goes through `say()` like anything else, so
the echo guard remembers it and the microphone will not transcribe JARVIS hearing itself -
that part is free. And if the agent later passes the same words to `converse()`, they are
recognised and not spoken twice. What overhearing cannot do is make the agent listen again:
the utterance the user speaks next lands in the transcript and waits there, and nothing
reads it until the agent calls `converse()` of its own accord. So this turns silence into an
answer, which is most of the value, and leaves the turn ended either way.

The one thing it cannot do is listen. Speaking overheard prose delivers the reply but does
not reopen the microphone, so the conversation stops there; `converse()` is still the only
thing that keeps it going, and the guide says so. Which makes this a safety net rather than
a route, and the right shape for a safety net: invisible when things work, and the
difference between an answer and silence when they do not.

**What is genuinely left, stated plainly.** Sampling is the only mechanism in MCP that
would truly enforce this: `sampling/createMessage` lets the server ask the client to run a
completion, so `converse()` could obtain the reply itself and speak it without ever
returning - no turn boundary for the agent to end. It was not built, for two reasons.
Whether a given client advertises `sampling` is unknown until it connects, which is why
capabilities are now logged on the first tool call. And on a single-slot llama-server it
would thrash the KV cache: an 80k agent prompt and a 500 token sampling prompt evicting
each other means reprocessing the agent's prompt every turn. It also reintroduces JARVIS
composing speech, which is the thing removed further up this file.

Elicitation and `InputRequiredResult` do not help - both route to the human, not to the
model. Absent sampling, an agent that ends its turn with prose and calls nothing at all is
reachable only by the error flag, and that is a client behaviour rather than a guarantee.

**Name the decision, not the mechanism.** The pair used to be `say` and `wait_for_speech`,
and the second name stopped being true the moment `then="listen"` became the ordinary way to
hear someone: it read as the canonical listening primitive while actually being the minority
path. `stay_silent` fixed that by naming the choice rather than the mechanism.

`converse` finishes the job, and this is a change of mind on the record. Asked directly
whether the two should collapse into one call by that name, the answer here was no - `say`
was short and blunt, rule 1 leaned on that, and `converse(text, then="keep_working")` looked
self-contradictory. Two failed sessions later, the objection was about wording and the
problem was about how many decisions a model makes at the end of a turn. `then="keep_working"`
is a conversation with a pause in it, which is not a contradiction, and one tool that always
looks the same beats two that are each individually well named.

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
debugging a misclick, and `screen.send_image` will send it to a model that can read it.

**Separate tools rather than one action verb.** The obvious shape is one `act(action,
target, text)` call. It is worse here, because the schema can then only say "text is
sometimes required" - where `click(target, expecting)` and `type_text(target, expecting,
text, then)` each state exactly what their own call needs, and a missing argument is
rejected before the tool body runs. Same reasoning as `converse(say, then)`.

**`expecting` is checked, the way an empty `say` is.** A target number is
worthless on its own: a number from a scan taken before the list scrolled still resolves
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
4k tokens, which any agent context can afford. And when it does happen the cut is an even
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
and play, an agent pressed the Windows key, scanned what came up, and got one element: a
target labelled "Search box" whose rectangle was the entire Start panel. Its centre is
therefore not a control, so the point check refused three type_text calls in a row - each
time correctly, each time with the same words, and each time the agent rescanned and got
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
the box. An agent cannot discover the flag on its own; the best it can do is relay a message
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

**One automation object per thread.** COM apartments do not share objects and an MCP
client may call one tool from a different thread than the last. Rather than marshal,
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

**The guide is a copy, and a copy goes stale.** The one failure mode in this whole design
that no schema can reach. `jarvis.md` gets copied into the agent's rules directory so it is
read every turn, and once the tools are renamed that copy is actively lying - it names tools
that do not exist and shows call shapes that were removed. The model weighs authoritative
prose against a schema and the prose wins, so it reverts to the loop the new signatures were
built to delete, and nothing in the transcript says why. Observed here with a guide three
days old, still teaching `wait_for_speech()` and a bare `say(answer)`. Hence `jarvis rules`,
which just compares the two files: cheap, and the only way the drift is visible at all.

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
  next calls `converse`. An MCP notification could improve that, if clients honour it
- Nothing fills the silence if the agent forgets to speak before slow work. That is
  deliberate - see above - but it does mean a forgetful agent sounds broken
- Screen control has no undo and no dry run. `expecting` catches the wrong target but
  nothing catches the right target with the wrong intent
- Nothing reads text out of a control. An agent can see that an edit box exists and type
  into it, but not what is already in it, so "read me the last message" is out of reach
- The scan is per window. Anything spanning two windows, or a dialog opening over the one
  being scanned, needs a second look to notice
- An application with no usable accessibility tree - a game, a canvas, a remote desktop
  window - is invisible to all of this, and turning on `send_image` does not rescue it:
  with no targets there is nothing to number. That case is what a vision model detecting
  controls in pixels is actually for, and it is not built here
