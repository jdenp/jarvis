"""JARVIS with the microphone taken out.

The same loop, the same tools, the same memories and the same terminal - typed at
instead of spoken to. It exists for two reasons. Over SSH there is no audio device
and no desktop session to drive, so this is what a phone on the sofa gets. And a
voice session is a bad place to work out why the model did something: you cannot
scroll back, and every experiment costs a sentence read out loud.

Nothing here is a second implementation. `ConsoleVoice` has the same two methods
`ServiceVoice` does, and `Brain.run_forever` cannot tell them apart. The drawing
is `ui.py`, shared with voice mode, so the two look the same.

The one difference on purpose: `brain.max_steps` is uncapped here. It exists
because somebody listening is spending patience on every tool call; somebody
watching a screen fill with edits is not, and a task that genuinely needs
sixty tool calls should get sixty rather than an answer cut short at sixteen.
"""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import replace

from . import __version__
from . import ui as terminal
from .brain import Brain, Model, ModelUnavailable

HELP = """\
Type and press enter. Anything else works exactly as it does out loud.

  /tools      what it can do
  /memories   what it has learned so far, about the desk and about you
  /forget     the file to edit, and where it is
  /quit       or Ctrl+D
"""


class Quit(Exception):
    """The user asked to leave."""


class ConsoleVoice:
    """Ears and mouth made of a keyboard and a screen.

    On a real console this borrows `typed.Typing`, the same background reader
    the voice app uses: a thread polls the keyboard without blocking, and
    lines land in a queue rather than being read with `input()`, which owns
    the terminal until Enter is pressed. That is what makes `hear(0.0)` - the
    loop's mid-task check for someone talking over the work - able to return
    something instead of always coming back empty, and it is what lets escape
    reach `brain.cancel()` while a reply is still being written.

    Piped or redirected, there is no console to poll and no live line to draw
    a background prompt on, so `start()` leaves `_typing` unset and `hear`
    falls back to a plain blocking `input()` - the same as before, one line
    read at a time, with no barge-in.
    """

    def __init__(self, ui=None, brain: Brain | None = None) -> None:
        self.ui = ui or terminal.Silent()
        self.brain = brain
        self.spoken: list[str] = []
        self._lines: queue.Queue[str] = queue.Queue()
        self._quit = threading.Event()
        self._typing = None

    def start(self) -> None:
        """Start the background reader, if the terminal can show one."""
        if not self.ui.colour:
            return
        from .typed import Typing

        self._typing = Typing(self.ui, self._enqueue, on_cancel=self._cancel)
        self._typing.start()

    def stop(self) -> None:
        if self._typing is not None:
            self._typing.stop()

    def _cancel(self) -> bool:
        return self.brain.cancel() if self.brain else False

    def _enqueue(self, said: str) -> None:
        """Where a typed line lands, off the reader thread.

        A command runs here rather than being queued, the same as it always
        did - `/quit` cannot put anything sensible in a queue of things to
        say, so it sets a flag instead and `hear` raises `Quit` the next time
        it is asked for a fresh line rather than mid-turn.
        """
        if said.startswith("/"):
            try:
                self.command(said)
            except Quit:
                self._quit.set()
            return
        self._lines.put(said)

    def _drain(self) -> list[str]:
        said = []
        while True:
            try:
                said.append(self._lines.get_nowait())
            except queue.Empty:
                return said

    def hear(self, timeout: float) -> list[str]:
        if self._typing is None:
            return self._hear_blocking(timeout)
        if timeout == 0.0:
            return self._drain()
        if self._quit.is_set():
            raise Quit
        try:
            said = [self._lines.get(timeout=timeout)]
        except queue.Empty:
            return []
        return said + self._drain()

    def _hear_blocking(self, timeout: float) -> list[str]:
        if timeout == 0.0:
            return []
        while True:
            said = self.ui.ask("\nyou > ").strip()
            if not said:
                continue
            if not said.startswith("/"):
                return [said]
            self.command(said)

    def command(self, said: str) -> None:
        """The handful of things that are about the session, not the desk."""
        word = said.split()[0].lower()
        if word in {"/quit", "/exit", "/q"}:
            raise Quit
        if word in {"/help", "/?"}:
            self.ui.note(HELP)
        elif word == "/tools" and self.brain is not None:
            width = max(len(name) for name in self.brain.toolbox.names)
            for tool in self.brain.toolbox.tools.values():
                self.ui.note(f"  {tool.name.ljust(width)}  {tool.description.split('.')[0]}")
        elif word == "/memories" and self.brain is not None:
            self.ui.note(self.brain.remembered() or "Nothing learned yet.")
        elif word == "/forget" and self.brain is not None:
            from . import tools

            self.ui.note(f"Edit or delete lines in {tools.memory_file(self.brain.config)}")
        else:
            self.ui.note(f"No such command as {word}. /help lists them.")

    def say(self, text: str) -> None:
        self.spoken.append(text)
        self.ui.spoke(text)

    def hush(self) -> None:
        """Nothing to cut off. Written already is written."""

    def waiting(self) -> str:
        """Nothing, because the prompt is about to be drawn on that line."""
        return ""


def run(config, verbose: bool = False) -> int:
    """One interactive session. Needs no microphone and no voice service."""
    # Uncapped: see the module docstring for why code mode does not take the
    # voice path's step budget.
    config = replace(config, brain=replace(config.brain, max_steps=0))
    ui = terminal.Ui()
    model = Model(config.brain, terminal=ui)
    if why := model.available():
        print(f"No model at {config.brain.url} - {why}", file=sys.stderr)
        print("Start llama-server, or point brain.url somewhere that answers.", file=sys.stderr)
        return 2

    voice = ConsoleVoice(ui)
    brain = Brain(config, voice, model=model, terminal=ui)
    voice.brain = brain
    voice.start()

    notes = [
        f"code mode - {config.brain.url}, {len(brain.toolbox.names)} tools",
        "/help for what else there is",
    ]
    if not verbose:
        notes.append(f"tool results and errors in {config.log_dir / 'jarvis.log'}")
    if voice._typing is not None:
        notes.append("type while it is working to steer it, escape to cancel")
    ui.banner(__version__, notes)

    try:
        brain.run_forever()
    except (Quit, EOFError, KeyboardInterrupt):
        ui.note("\nGoodbye, sir.")
    except ModelUnavailable as exc:
        ui.warn(f"\n{exc}")
        return 2
    finally:
        voice.stop()
        ui.close()
    return 0
