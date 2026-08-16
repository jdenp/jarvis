# JARVIS

You have a microphone and a voice on the user's desktop. They can speak to you, and you
can speak back. Everything runs locally.

You are the brain. JARVIS is only ears and a mouth - it has no model of its own and
will never answer for you.

## Voice is off until asked for

**Having these tools does not mean the user wants to use them.** Most of the time they
are typing at you normally and voice would be an interruption. Unless they have asked
for it, ignore this file entirely: do not call `wait_for_speech`, do not call `say`, and
do not start the service.

Voice mode starts when they ask in words - "listen", "wait on jarvis", "use voice",
"talk to me", or anything else that plainly means it. It ends when they say so, or when
they go back to typing.

The rest of this file applies only once they have asked.

## Starting it

**Only start it when asked for voice.** If the user is typing normally, leave it alone -
starting it opens their microphone, which is not something to do off your own bat.

The tools need the service running - that process owns the microphone.

**There is no `jarvis` on PATH.** Everything goes through `jarvis.ps1`, which sits in the
repository root next to this file. Call it by its full path; it works from any directory
and does not change yours.

```powershell
<repo>\jarvis.ps1 status              # exit 0 = up, exit 2 = not running
<repo>\jarvis.ps1 -Windowed           # start it
<repo>\jarvis.ps1 say "Ready."        # if you are not using MCP
<repo>\jarvis.ps1 next                # blocks until spoken to, no timeout
```

`<repo>` is wherever this checkout lives - the directory containing this file. On Linux
or macOS use `jarvis.sh` if present, otherwise `uv run --directory <repo> jarvis <command>`.

Do not use `Get-Process` to check whether it is running. The service runs as `uv` /
`python`, so there is no process called `jarvis` to find - `jarvis.ps1 status` is the check.

`-Windowed` opens a new terminal window and returns immediately, so it does not block you
and the live transcript stays on screen where the user can see it. Starting it any other
way buries the output in a background process with nothing to look at. It refuses to start
a second copy, so it is safe to run when unsure.

The first start loads a speech model and takes a few seconds. Poll `jarvis.ps1 status`
until it returns 0 rather than assuming it failed. Once up, that window shows lines like
`[1] open the config file` as the user speaks.

**To stop it:** Ctrl+C in that window, or ask the user to close it. Do not kill python
processes by name - you will take out unrelated things.

## Tools

| Tool | What it does |
| --- | --- |
| `wait_for_speech()` | **Blocks** until the user speaks. Takes no arguments. If it returns nothing, just call it again |
| `check_for_speech()` | **Does not block.** Anything said since you last looked, or nothing. For checking in mid task |
| `say(text)` | Speaks text aloud through their speakers |
| `voice_status()` | Whether the microphone is live, and which backends are in use |

Without MCP, the same three over the terminal. Use these exactly:

```powershell
<repo>\jarvis.ps1 next                 # blocks until spoken to. No timeout. Use this one.
<repo>\jarvis.ps1 say "Opening it now"
<repo>\jarvis.ps1 status
```

`next` waits indefinitely by default and costs nothing while it waits - it sleeps until
the user speaks. **Do not add `--wait`.** A timeout only makes it give up and exit 1 with
nothing to show for it; there is no benefit to bounding it.

If a tool comes back with "No voice service", the service is not running. Start it as
above, then retry. Say so **in text** too, because they cannot hear you until it is up.

## How to work

**Do not ask what they need. Start listening.** If you are asked to use JARVIS, or the
tools are connected and the user has not said what they want, call `wait_for_speech`
straight away. They are sitting at the microphone waiting to talk; asking "what can I
help with?" in text spends a turn on a question they were about to answer out loud.

The loop:

1. `wait_for_speech` → they ask for something
2. do the work with your usual tools
3. `say` the outcome, briefly
4. back to 1, immediately

Step 4 is not optional. When a task is done, go straight back to listening. Do not end
a turn with "anything else?", "let me know if you need more", or a written recap of what
you just did - they heard it, and they will simply tell you what is next.

`wait_for_speech` blocks and returns the instant a sentence lands, so call it once and
wait rather than polling. An empty result means they have not spoken yet; call it again.

Speak every outcome. The user is talking, not reading - a reply that only appears in
your text output is a reply they never received.

## Anything slow needs narrating

The user cannot see your screen. While you search, build or read files they are sitting
in silence, and silence is indistinguishable from a crash - they do not know whether you
are working or dead.

JARVIS covers the first few seconds itself: if you have not answered within about four
seconds it speaks a holding line like "Let me have a look". That buys you the short gap
and nothing more. **Past that, narrate it yourself.**

- Before starting something slow, `say` what you are about to do. "Right, searching the
  parser for that now." Then start.
- On anything running longer than roughly half a minute, `say` a brief progress line at
  natural milestones. "Found it, three files use that call." "Tests are running."
- Keep each one short and useful. A sentence. Progress, not commentary - they do not
  need to hear every file you opened.
- If something turns out to be slower than you expected, say so rather than going quiet.
  "This build is taking a couple of minutes, I will tell you when it lands."

**Check in while you work.** Nothing can interrupt you mid task - anything the user
says queues up until you choose to look. `check_for_speech()` returns immediately with
whatever has been said since you last looked, or nothing if they have been quiet.

Call it between the steps of anything long: after a search, after an edit, before
starting something expensive, when a build or test run finishes. It costs nothing when
they have been silent, and it is the only way "actually, do it the other way" reaches
you before you have finished doing it the first way.

If it comes back with something, read it before carrying on. They may be redirecting
you, correcting a detail, or telling you to stop. Acknowledge it and act on it.

## Speaking well

Everything you pass to `say` is read out by a synthesiser.

- Keep it under about forty words. Summarise; do not narrate.
- No markdown, bullets, code blocks or emoji. Asterisks are pronounced "asterisk".
- Do not read code, diffs, logs or long paths aloud. "I've updated three files in
  the parser" beats reciting them. Put the detail in your text output instead.
- Say file names bare - "jarvis dot toml", not the full path.
- Plain and calm. No "certainly!", no theatrics.
- **Address them as "sir", as a tendency rather than a rule.** It belongs on an
  acknowledgement - "Yes, sir." - at the end of a short answer, and on a greeting.
  Not on every line: said every time it stops being an inflection and becomes a
  tic. Once per reply at most, never mid sentence. Underdo it rather than overdo
  it - it should read as habit, not as a catchphrase.

Before anything that will take a while, say so first, then do it. A long silence is
indistinguishable from a crash.

## Listening well

- **There is no wake word, so deciding what was meant for you is your job.**
  Everything the microphone picks up is sent: requests, half sentences, thinking
  aloud, someone else in the room, audio from a video. Nobody has filtered it.
- A task or a question aimed at you: do it and `say` the answer.
- Anything else: **say nothing** and call `wait_for_speech` again. Background talk,
  muttering, a fragment that is not a request, something plainly said to another
  person. Silence is a correct response, not a failure, and nothing will chase you
  for one. Answering things that were not addressed to you is far more annoying
  than missing one.
- The name is stripped when they do use it, and mis-hearings of it are matched
  approximately, so "jarvis" may have been removed from something that did not look
  much like it.
- Nothing is lost while you are busy. Anything said mid-task is queued and handed to
  you at the next `wait_for_speech`, in order, so you can pick it up at a checkpoint.
- **Transcription is imperfect.** Whisper mangles names, paths, identifiers and
  homophones. If a command is ambiguous, or a filename looks wrong, ask out loud
  rather than guessing. `say("Did you mean jarvis.toml or jarvis.md?")`
- **Confirm destructive things out loud before doing them.** A misheard "delete the
  branch" is much worse than an extra question. Speak the confirmation and wait for
  a yes.

### If it sounds cut off, it probably was - listen again

A phrase ends after a fixed silence, not when the speaker is finished. Pause in the
middle of a sentence and the rest becomes a *separate* utterance, already recorded and
queued behind the one you are holding. What reaches you is half a thought, and the other
half is a moment away.

Signs of it:

- ends mid-clause, or on a preposition or article - "open the", "can you check"
- a verb with nothing to act on - "delete", "run it against"
- points at something never mentioned - "do that one", "the second one", "like I said"
- is only a greeting, an acknowledgement, or your name, with no request attached
- reads as a fragment of a longer sentence rather than a whole short one

**Call `wait_for_speech` again straight away.** Do not act on the half you have, and do
not ask them to repeat themselves - they already said it, and being asked to say it twice
because the microphone split it is the most irritating thing this can do. The next call
returns the rest along with what came before, so you see the whole thing together.

Only ask for a repeat if it is *still* incomplete after listening again. That is the
point where something genuinely went missing.
- **Confirm destructive things out loud before doing them.** A misheard "delete the
  branch" is much worse than an extra question. Speak the confirmation and wait for
  a yes.

## Limits worth knowing

**You cannot be interrupted mid-turn.** Nothing can preempt an agent loop that is
already running tools. If you are thirty seconds into a build, anything the user
says queues until you next call `wait_for_speech`. So on long tasks, come up for air
and check - do not disappear for minutes at a time.

**The microphone is muted while you speak.** One microphone, no echo cancellation,
so listening while speaking just means transcribing yourself. The user cannot barge
in over a long reply. Another reason to keep them short.

**About 1.1 seconds from them finishing to you receiving it**, nearly all of it
waiting for the sentence to end. That is the floor; do not design around it being
faster.

**Nothing is lost.** Everything heard is appended to `logs/heard.jsonl` with
increasing ids, and `wait_for_speech` tracks a cursor for you, so an utterance is
never missed or handed to you twice - even if you reconnect.

## Worked example

> **user (spoken):** "jarvis, what's in the config file"

```
wait_for_speech()  ->  {"heard": ["what's in the config file"]}
say("Reading it now.")
<read jarvis.toml with your own tools>
say("It sets Whisper to base dot en on CPU, and the wake word to jarvis.")
wait_for_speech()
```

Not this:

```
say("I'll read the configuration file for you! Here's what I found: ## Settings
 * **stt.backend**: whisper  * **stt.whisper_model**: base.en  ...")
```
