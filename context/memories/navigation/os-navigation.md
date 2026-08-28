# Driving a Windows desktop

Reference rather than memory: how Windows behaves, which is the same on every
machine and ships with the project. `user-navigation.md` beside it is where
JARVIS writes what it works out for itself, and anything there that turns out to
be general belongs here instead - by hand, because that is the difference
between the two files.

Every line is a bullet, because the bullets are what get read into the prompt.
The prose around them is for whoever opens the file.

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
- To open an application, press the Windows key, type its name and press enter.
  It needs no scan, it finds anything installed rather than only what is pinned,
  and it works for things with no name on the command line.
- The Start menu exposes one element covering itself, so a scan of it comes back
  with nothing clickable. That is expected: type wherever the caret already is.
- Reach for a command line only after that fails. Plenty of what people run has
  no name on the path, and "where" finding nothing does not mean it is missing.
- The taskbar scans as a window called Taskbar, one target per pinned
  application. Useful for seeing what is already running, and it only ever
  covers what somebody pinned.
- Moving a window needs no scan and no title bar button: win+up maximises what
  is in front, win+left and win+right put it against one side.
- Minimising takes win+down twice from a maximised window. The first press only
  restores it to its old size, which looks like nothing having happened. Expect
  two, and be surprised only if it is still there after the second.
- The media keys need no window either - playpause, nexttrack, prevtrack, stop,
  volumeup, volumedown, mute. Windows routes them to whatever is playing.
- Look before acting and again afterwards. Anything you do redraws something and
  the numbers move; a number from the scan before is refused rather than
  clicked, but a refusal still costs a turn.
- Some windows report a full tree of coordinates while minimised, left over from
  wherever they were last drawn. Focusing one is the only thing that fixes it.
- A window that reports elements but no targets is still building itself. Wait
  and look again rather than deciding it is empty.
- Close what you opened. A menu left up is a job half finished, and escape
  closes most of them.
