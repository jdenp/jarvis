"""JARVIS with the microphone taken out.

The same loop, the same tools, the same memories and the same terminal - typed at
instead of spoken to. It exists for two reasons. Over SSH there is no audio device
and no desktop session to drive, so this is what a phone on the sofa gets. And a
voice session is a bad place to work out why the model did something: you cannot
scroll back, and every experiment costs a sentence read out loud.

Nothing here is a second implementation. `ConsoleVoice` has the same two methods
`ServiceVoice` does, and `Brain.run_forever` cannot tell them apart. The drawing
is `ui.py`, shared with voice mode, so the two look the same.
"""

from __future__ import annotations

import sys

from . import __version__
from . import ui as terminal
from .brain import Brain, Model, ModelUnavailable

HELP = """\
Type and press enter. Anything else works exactly as it does out loud.

  /tools      what it can do
  /memories   what it has learned on this machine
  /forget     the file to edit, and where it is
  /quit       or Ctrl+D
"""


class Quit(Exception):
    """The user asked to leave."""


class ConsoleVoice:
    """Ears and mouth made of a keyboard and a screen.

    `hear(0.0)` is the loop's mid-task check for someone talking over the work.
    There is nothing to check here - one line is read at a time - so it returns
    nothing, and the barge-in the voice path gets is absent rather than faked.
    """

    def __init__(self, ui=None, brain: Brain | None = None) -> None:
        self.ui = ui or terminal.Silent()
        self.brain = brain
        self.spoken: list[str] = []

    def hear(self, timeout: float) -> list[str]:
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

    def waiting(self) -> str:
        """Nothing, because the prompt is about to be drawn on that line."""
        return ""


def run(config, verbose: bool = False) -> int:
    """One interactive session. Needs no microphone and no voice service."""
    ui = terminal.Ui()
    model = Model(config.brain, terminal=ui)
    if why := model.available():
        print(f"No model at {config.brain.url} - {why}", file=sys.stderr)
        print("Start llama-server, or point brain.url somewhere that answers.", file=sys.stderr)
        return 2

    voice = ConsoleVoice(ui)
    brain = Brain(config, voice, model=model, terminal=ui)
    voice.brain = brain

    notes = [
        f"chat mode - {config.brain.url}, {len(brain.toolbox.names)} tools",
        "/help for what else there is",
    ]
    if not verbose:
        notes.append(f"tool results and errors in {config.log_dir / 'jarvis.log'}")
    ui.banner(__version__, notes)

    try:
        brain.run_forever()
    except (Quit, EOFError, KeyboardInterrupt):
        ui.note("\nGoodbye, sir.")
    except ModelUnavailable as exc:
        ui.warn(f"\n{exc}")
        return 2
    finally:
        ui.close()
    return 0
