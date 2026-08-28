# What JARVIS can do

Generated from `tools.py` by `jarvis tools --write`, with every feature switched
on. A test fails if the two drift.

Nothing reads this file. The model is sent these descriptions as JSON schemas on
every single call, so there is nothing here for it to remember and nothing for it
to load - this is the same text, written out for a human. It exists because after
the system prompt these descriptions are the largest influence on what JARVIS
does, and reading them should not mean reading Python.

The wording is deliberate throughout. Each one says what the tool refuses and
why, because a model that knows the shape of a refusal asks for the right thing
the first time. To change a description, change `tools.py` and regenerate: the
prose and the signature live together on purpose, since prose that drifts from a
signature is believed over it.

## look_at_screen

List everything on screen that can be clicked or typed into, numbered. With no arguments it reads the window in front; `window` picks another by any part of its title, and `matching` keeps only labels containing it, which is how you find one control in a crowded window. You get numbers and labels, never coordinates. Look again after anything you do.

- `window` (string) - part of a window title
- `matching` (string) - only labels containing this

## focus_window

Raise a window, restoring it if it was minimised, then scan it. Input goes to whatever holds the foreground, so this is what to call when what you want is behind something else, and the only thing that gets at a minimised window.

- `window` (string, required) - part of a window title
- `matching` (string) - only labels containing this

## click

Click one of the numbers from look_at_screen. `expecting` is what is inside the quotation marks beside that number, not the role in front of them, and it is checked first: if the number now points at something else the click is refused rather than landing on it. This is the real pointer on the real desktop.

- `target` (integer, required) - a number from look_at_screen
- `expecting` (string, required) - the label beside that number
- `button` (left | right)
- `clicks` (1 | 2)

## type_text

Type text. `then` decides whether it is submitted: press_enter sends the message or runs the search, leave_it types and stops - a half written message sent early cannot be taken back.

Name a `target` and it is clicked first to put the caret there, with `expecting` checked exactly as click does. Leave `target` out and the text goes wherever the keyboard focus already is, which is what you want for something that just opened with its caret ready - the Start menu, a dialog, a search bar that took focus on its own - and the only way into a window with nothing clickable in it. `clear_first` selects what is there so the text replaces it.

- `text` (string, required)
- `then` (press_enter | leave_it, required)
- `target` (integer) - optional, from look_at_screen
- `expecting` (string) - required with target
- `clear_first` (boolean)

## scroll

Wheel over a target and scroll. Use it when what you want is not in the list because it is scrolled out of view - offscreen elements are left out of a scan rather than offered at coordinates nobody can click.

- `target` (integer, required)
- `expecting` (string, required)
- `direction` (up | down, required)
- `notches` (integer)

## press_keys

Press a combination like ctrl+s, alt+f4, escape or f5, at whatever holds the keyboard focus. An unknown key name is refused rather than half pressed.

Two sets are worth reaching for before anything else, because neither needs a window, a scan or a target. The media keys - playpause, nexttrack, prevtrack, stop, volumeup, volumedown, mute - which Windows routes to whatever is playing. And the window keys: win+up maximises whatever is in front, win+down restores or minimises it, win+left and win+right put it against one side. That is how a window gets moved around; hunting for a title bar button is not.

- `keys` (string, required) - e.g. ctrl+s, playpause

## pause_transcription

Stop listening. The microphone stops being read, so nothing is transcribed, logged or recorded until it is resumed - not merely withheld from you.

For when they ask for privacy, or say they are on a call, or are about to have a conversation that is not with you.

Call it FIRST and say so afterwards. Your words end your turn, so a reply that promises to stop listening is a promise instead of the act - and once it is done, say that the num lock key brings you back, because from then on you cannot hear them ask. Not a way to avoid answering something: a hyphen does that and keeps your ears.

No arguments.

## resume_transcription

Start reading the microphone again. Nothing said during the pause is recoverable - it was never captured.

No arguments.

## search_web

Search the web and get back a few results: a title, the site, a sentence of each and the address. For anything you cannot know from here - the news, a score, an opening time, a fact you are not sure of.

The snippets often answer the question on their own, and then you just say the answer. Open one with read_page only when they do not, because a page is slow and long. Say where an answer came from when it is the sort of thing that could be wrong, and never repeat a result you did not get.

- `query` (string, required) - what to search for

## read_page

Fetch one web page and return its text, markup stripped and cut short. Use it on an address from search_web when the snippet was not enough.

It reads what the server sends, so a page that builds itself in the browser comes back empty - not something to retry, something to say. And they are listening: summarise it in a sentence, never read it out.

- `url` (string, required) - the address to read

## remember

Write down one thing you have learned about this machine, so you have it next time. Your whole list is read back into your prompt at the start of every turn.

This is for how the desk behaves, and most of it is only discoverable by getting it wrong: a window whose tree is empty until it has been focused, an application that takes a moment to build itself, which of four identically labelled buttons is the one that works, a command that turned out to be the way to do something. When a tool refuses you and you work out why, that is exactly what this is for.

Not for anything about one conversation - not what they asked for, not what you replied, not what they like. One sentence, and specific enough to act on months from now: a number from a scan will be wrong by then, a label or a window name will not.

- `lesson` (string, required) - one sentence

## run_command

Run a PowerShell command and return its output. This is everything the desktop tools are not: files, git, winget, curl, any program on PATH.

Editing source files a line at a time through this is not your job and not something you are any good at. Say so and leave it to whoever asked.

It waits for the command to finish, so nothing interactive: no prompts, no pagers, no servers held in the foreground. Anything that changes the machine, say what you did once it is done - your words end your turn, so announcing it first means it never happens.

- `command` (string, required)
