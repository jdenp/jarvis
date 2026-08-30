# Driving a Windows desktop

Reference rather than memory: how Windows behaves, which is the same on every
machine and ships with the project. `memories.md` is where JARVIS writes what it
works out for itself, and anything there that turns out to be general belongs
here instead - by hand, because that is the difference between the two files.

Every line is a bullet under a `##` heading, because the bullets and the
headings over them are what get read into the prompt. The prose around them is
for whoever opens the file.

## Files and the shell

- Files are a shell question, not a pointer question. Finding, listing,
  reading, copying, renaming and deleting are all one run_command:
  Get-ChildItem, Test-Path, Get-Content, Move-Item. Exact names, no scrolling,
  no window that redraws under you, and it works on folders nobody has open.
- File Explorer is for showing somebody a file, not for finding one. Clicking
  through Quick Access to a Desktop folder is several minutes and a dozen
  refusals for something Get-ChildItem answers in one call.
- The same goes for anything else with a text answer: what is installed, what is
  running, how much disk is left, when something was last changed. The pointer
  is for applications that only exist as a window.
- A file dialog is a window of its own called "Open" or "Save As", not part of
  whatever raised it. Focus it by that name and read its numbers off its own
  scan. Working it off the browser's scan is the single biggest waste of a call
  here: the dialog's controls do appear there, with the browser's numbers, and
  typing into those puts the text nowhere.
- The whole of a file dialog is the "File name:" box. Type the full path into it
  and submit - type_text(target=.., text="C:\\Users\\you\\Desktop\\thing.zip",
  then="press_enter"). It opens that file from wherever the dialog happens to be
  sitting, so there is no Quick Access to click through and no breadcrumb to
  find, and it works the same in every application.
- Get the path from the shell first. Get-ChildItem gives the exact name and the
  box takes it verbatim, so uploading a file is two calls: find it with a
  command, type it into the dialog.
- A dialog's list of files mostly does not come back in a scan - the buttons and
  the Quick Access tree do, the files do not. Scrolling to look for them finds
  nothing however far you go, which is the other reason the path goes in the
  name box.
- A window called "Open" that scans as two or three targets is a message box on
  top of the dialog, not the dialog. Answer it and look again; the dialog is
  still underneath.
- A file dialog closes when it accepts. Check the destination for what you sent,
  not the dialog for whether it went - an upload appears as a "Cancel upload"
  button and then as the file, and f5 settles a page that has not caught up.
- In a web file list - Drive, a mail attachment pane, anything of that shape - a
  click only selects. Enter is what opens it. Double clicking works on the
  desktop and mostly does not there.

## Applications

- To open an application, press the Windows key, type its name and press enter.
  It needs no scan, it finds anything installed rather than only what is pinned,
  and it works for things with no name on the command line.
- A web page is a shell job, and giving the browser the address is the whole of
  it: `start chrome https://youtube.com`. That opens the page in the last used
  profile with no profile picker, no new tab to find and no address bar to hunt
  for. Never open a browser and then try to drive it to a page.
- Chrome started with no address shows the profile picker. Each card scans as
  "Open Casual profile", and the small button beside it is "More actions" - the
  three dots, which only offers Edit and Delete. Press the card, not the dots.
  `--profile-directory="Profile 1"` skips the picker for a named profile.
- A browser tells the accessibility tree almost nothing about the page it is
  showing. A whole window scans as about thirty elements with no address bar
  among them, so ctrl+L is how you reach it - no scan, every browser.
- The Start menu exposes one element covering itself, so a scan of it comes back
  with nothing clickable. That is expected: type wherever the caret already is.
- Reach for a command line only after that fails. Plenty of what people run has
  no name on the path, and "where" finding nothing does not mean it is missing.
- The taskbar scans as a window called Taskbar, one target per pinned
  application. Useful for seeing what is already running, and it only ever
  covers what somebody pinned.

## Windows

- Moving a window needs no scan and no title bar button: win+up maximises what
  is in front, win+left and win+right put it against one side.
- Minimising takes win+down twice from a maximised window. The first press only
  restores it to its old size, which looks like nothing having happened. Expect
  two, and be surprised only if it is still there after the second.
- The media keys need no window either - playpause, nexttrack, prevtrack, stop,
  volumeup, volumedown, mute. Windows routes them to whatever is playing.
- Look before acting and again afterwards. Anything you do redraws something and
  the numbers move; a number from the scan before is refused rather than
  clicked, but a refusal still costs a call.
- Some windows report a full tree of coordinates while minimised, left over from
  wherever they were last drawn. Focusing one is the only thing that fixes it.
- A window that reports elements but no targets is still building itself. Wait
  and look again rather than deciding it is empty - unless it runs as
  administrator, in which case waiting never helps and the scan says so.

## Elevated windows

- Anything running as administrator is out of reach. Task Manager, an admin
  terminal, regedit: Windows shows an unelevated process one element and no
  targets, refuses its clicks and its keystrokes silently, and swallows the
  hotkey while such a window has focus. Nothing about that changes with time,
  and it is not the window still loading.
- So say so. "That is a window I cannot touch" is the honest answer and it takes
  one sentence. Four minutes of focusing, scrolling, screenshotting and alt+f4
  is the wrong answer given at length.
- If one has to be closed, taskkill /F /IM name.exe through run_command does it,
  and they will get a prompt from Windows to approve. Worth mentioning before
  running it, because somebody has to be at the desk to click it.

## Closing things

- To close a program, name the window: press_keys(keys="alt+f4",
  window="Google Chrome"). Sent without a window it goes wherever the focus
  happens to be, and looking at a window does not focus it - that is how JARVIS
  once closed its own console instead of the browser. Stop-Process -Name chrome
  through run_command needs no focus at all and is surer.
- Close what you opened. A menu left up is a job half finished, and escape
  closes most of them.
