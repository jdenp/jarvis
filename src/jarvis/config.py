"""Runtime configuration.

Precedence, lowest to highest: dataclass defaults, the config file, ``JARVIS_*``
environment variables, command line flags. The defaults here are the source of
truth; ``config/defaults.json`` is generated from them and a test catches drift.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any


def under_root(named: str) -> Path:
    """A configured path, with relative names read from the project root."""
    path = Path(named).expanduser()
    return path if path.is_absolute() else project_root() / path


def project_root() -> Path:
    """Directory holding jarvis.toml and logs/, overridable with JARVIS_HOME."""
    if env_home := os.environ.get("JARVIS_HOME"):
        return Path(env_home).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AudioConfig:
    """Microphone capture settings."""

    device_index: int | None = None
    # Hard cap on one phrase, only reached if noise is unbroken enough that the
    # phrase never ends on silence. High, because being cut off mid sentence is
    # worse than waiting - PhraseEnd is what keeps the wait from happening.
    phrase_time_limit: float = 60.0
    # How a buffer is judged to be speech. silero scores each frame with a 1.2MB
    # network, so a footstep as loud as a word still scores near zero; energy is
    # loudness alone and cannot tell them apart. auto falls back to energy.
    vad: str = "auto"  # auto | silero | energy
    # Silero's cutoff for deciding speech has started. Lower hears more.
    vad_threshold: float = 0.35
    # Once speaking, how far the score may dip before it counts as a pause.
    # Silero's own implementation does this, and without it a quiet consonant
    # mid word reads as silence.
    vad_hysteresis: float = 0.15
    # Least speech that counts as a phrase rather than a noise. Measured in real
    # speech now, so this is stricter than it looks - too high and short answers
    # like "yes" are dropped without trace.
    min_speech_seconds: float = 0.15
    # The four below are energy mode only. Silero ignores loudness entirely.
    calibration_seconds: float = 1.5
    dynamic_energy_threshold: bool = True
    energy_threshold: float | None = None
    # Floor under calibration - a silent room calibrates low enough to hear its
    # own speakers. Raise it if JARVIS hears itself, lower it if you must shout.
    min_energy_threshold: float = 55.0
    # Non-speech that ends a phrase, and the floor under the delay before an
    # agent sees you spoke. Raise it if you keep getting cut off mid sentence.
    pause_threshold: float = 1.2
    # Noise allowed inside that pause without restarting it: 0.85 tolerates
    # 0.32s of clicks or a distant voice. Measured against rendered speech,
    # 0.85 cut 1 sentence in 5 short, 0.8 cut 2, and 0.8 buys only 0.06s more
    # tolerance for it. It does not shorten the pause itself. See PhraseEnd.
    pause_quiet_fraction: float = 0.85
    # How long after JARVIS stops talking to keep ignoring the microphone.
    echo_guard_seconds: float = 0.5
    # Full duplex: the microphone stays open while JARVIS talks, so a reply can
    # be cut off mid sentence - late, once the phrase has ended, rather than on
    # the first syllable. OFF, because on speakers it transcribes itself -
    # there is no acoustic echo cancellation here, and the only thing between
    # that and JARVIS answering its own voice is the text comparison in echo.py,
    # which has already been beaten once by a long reply. On headphones there is
    # nothing to hear and this is free; turn it on there.
    #
    # It costs less than it sounds. Talking over JARVIS while it is thinking
    # still works either way - the microphone is only shut while a reply is
    # actually being spoken, which is seconds at the end of a turn.
    #
    # Where it starts rather than where it stays: whether headphones are on is
    # a thing that changes during the day, so holding the hotkey or pressing the
    # headphone button on the web app flips it for the session.
    listen_while_speaking: bool = False


@dataclass(frozen=True)
class SttConfig:
    """Speech to text. Defaults to local transcription."""

    backend: str = "whisper"  # whisper (local) | google (uploads your audio)
    language: str = "en-GB"
    whisper_model: str = "base.en"
    # CUDA is far quicker on long utterances but costs ~340MB of VRAM. Set
    # "auto" if the GPU is free, and install it: uv sync --extra cuda
    whisper_device: str = "cpu"  # cpu | cuda | auto
    whisper_compute_type: str = "default"
    whisper_beam_size: int = 1
    whisper_vad: bool = True


@dataclass(frozen=True)
class TtsConfig:
    """Text to speech. Defaults to the offline Windows voice."""

    engine: str = "auto"  # auto (kokoro if downloaded, else sapi) | kokoro | sapi | edge | none
    voice: str = "en-GB-RyanNeural"  # edge only
    # Preference order, first installed wins. Hazel is en-GB, Zira the en-US fallback.
    sapi_voice: str = "Hazel, Zira"
    # Kokoro's voice. The b* ones are British and the a* American, which also
    # decides which phonemes the text is read with.
    kokoro_voice: str = "bm_george"
    # Downloaded once by hand, because they are 330MB and nothing should pull
    # that down behind your back. Relative names sit under the project root.
    kokoro_model: str = "models/kokoro-v1.0.onnx"
    kokoro_voices: str = "models/voices-v1.0.bin"
    # About 5x real time on a modern desktop CPU against 20x on CUDA, and one
    # sentence is well under a second either way - so cpu unless the GPU is
    # otherwise idle. Needs onnxruntime-gpu installed for cuda to be there.
    kokoro_device: str = "cpu"  # cpu | cuda | auto
    rate: int = 210
    volume: float = 1.0


@dataclass(frozen=True)
class ServiceConfig:
    """The voice service an agent connects to."""

    host: str = "127.0.0.1"
    port: int = 8770
    # Longest a single /heard may block before it returns empty. Caps the wait a
    # caller asks for, so a client with its own timeout asks again rather than
    # erroring.
    max_wait_seconds: float = 55.0
    transcript_file: str = "heard.jsonl"
    # A page this service serves, so a phone can be the microphone. It adds GET /
    # and the endpoints under it, and a second capture source that streams into
    # the same phrase splitter the room does.
    #
    # On. It was off, on the argument that anything opening a microphone should
    # be opted into - but it opens nothing on its own. Nobody outside this
    # machine can reach it until they have put `tailscale serve` in front of it
    # themselves, and a phone with no browser on it costs one idle capture
    # source that sleeps. Off, the page 404s with nothing to say why, which
    # looks exactly like the feature being broken.
    #
    # This service is still loopback and still has no auth, and that is the whole
    # design: put `tailscale serve` in front of it and let Tailscale say who you
    # are. Do not bind this to anything routable instead. The browser needs the
    # https that gives you anyway - a microphone is refused outright without it.
    start_webapp: bool = True
    # Key that shuts this microphone. Empty disables it. Avoid keys you type
    # with - nothing is swallowed, so whatever is chosen still does its normal
    # job everywhere else. Num Lock earns it by being a key nothing else wants;
    # the cost is that pausing also flips the numeric keypad, which is the same
    # keypress doing both things.
    #
    # The three lock keys are watched rather than hooked, which is why they work
    # with an elevated window in front and anything else does not. Holding one
    # for a second and a half is the other job: it flips listen_while_speaking.
    # Only the lock keys, because a hooked key has already fired by the time it
    # comes back up. See hotkey.py.
    hotkey: str = "num lock"


@dataclass(frozen=True)
class ScreenConfig:
    """Seeing the desktop, and acting on it.

    Looking is always allowed; it reads the accessibility tree and touches
    nothing. Acting moves the real pointer and types on the real keyboard, which
    is why this is a switch at all.
    """

    # On. It was off, on the argument that moving someone's pointer should be
    # opted into - but the whole point of the feature is to act, an agent cannot
    # discover the flag on its own, and the failure when it is off looks exactly
    # like the feature being broken: a live session spent four calls refusing to
    # touch a minimised window without the tool that would have restored it.
    # Set it false to get the read-only half back; look_at_screen and screenshot
    # never depended on it.
    control: bool = True
    # Most targets offered at once. 60 was chosen on the theory that a long list
    # makes a model guess; measured against real applications it was far worse
    # than that - Spotify has 166 real targets and Outlook in a browser 177, so
    # 60 hid most of both. At 200 nothing normal truncates at all, and 200
    # targets is around 4k tokens, which any agent context can afford. When it
    # does truncate the cut is spread rather than taken off the end.
    max_targets: int = 200
    # Older than this and a scan is refused rather than acted on. Windows move.
    max_scan_age_seconds: float = 60.0
    # Anything narrower or shorter than this is a divider, not a target.
    min_target_pixels: int = 6
    # Longest label kept per target. A chat row carries the whole last message
    # as its name, and sixty of those is the prompt this was meant to shrink.
    label_chars: int = 80
    # Between moving the pointer and pressing, so hover states settle.
    click_settle_seconds: float = 0.05
    # After raising a window, before scanning it. Restoring is animated, and an
    # element measured mid animation reports where it was, not where it lands.
    focus_settle_seconds: float = 0.35
    # Send the marked screenshot to the agent alongside the list, so a model that
    # can read images gets the picture and the numbers together. Shrunk to
    # screenshot_max_width first. It costs whatever the image costs on a model
    # that cannot see it - without --mmproj or its equivalent loaded, that is
    # payload for nothing, and this is the setting to turn off.
    # Where `screenshot` writes, under logs/. Overwritten each time - it is the
    # latest picture, not an album.
    screenshot_file: str = "screen.png"
    # Widest a screenshot is sent at, shrunk if wider. 0 keeps it full size. The
    # whole desk across two monitors is 4880px and 665KB, which is worth
    # narrowing before it goes anywhere.
    screenshot_max_width: int = 1600
    # Where `jarvis look --marks` writes the marked screenshot, under logs/. What
    # to look at when a click lands somewhere unexpected. Nothing draws one
    # automatically - a scan costs a full screen grab, about half a second, and
    # the numbered list is what the model reads.
    marks_file: str = "marks.png"


@dataclass(frozen=True)
class BrainConfig:
    """JARVIS's own agent loop, and the model behind it.

    With this on, JARVIS answers for itself: it hears you, calls a model with
    the desktop tools, and speaks the reply. Speaking is not a tool here - the
    model's reply IS what goes through the speakers - which is the whole reason
    this exists. See brain.py, and DESIGN.md for the five mechanisms it replaced.
    """

    # Any OpenAI-compatible chat endpoint. llama-server with --jinja is what
    # this was built against; --jinja is the part that parses tool calls.
    #
    # Required. JARVIS does not start without one, and there is no switch to
    # turn the brain off. It used to carry on as ears and hands, which meant an
    # unreachable model looked exactly like a working assistant that ignored
    # everything said to it - a worse outcome than a process that refuses to
    # start and says why.
    url: str = "http://127.0.0.1:8081/v1"
    # How long to wait for it at startup before giving up. Both this and the
    # model server start at login and nothing sequences them, so the ordinary
    # case is JARVIS winning the race and a 35B model taking a minute or two to
    # load off disk. Refusing to start there is refusing over a few seconds of
    # bad luck. It is checked every few seconds and says so while it waits; 0
    # goes back to failing at once. Only the service waits - `jarvis chat` is
    # somebody sitting at a keyboard, and a prompt that never comes back is
    # worse than being told the endpoint is down.
    wait_for_model_seconds: float = 600.0
    # Sent as `model`, and llama-server ignores it - it serves whatever was
    # loaded. Only matters for an endpoint that hosts more than one.
    model: str = "local"
    # Bearer token, for an endpoint that wants one. Loopback does not.
    api_key: str = ""
    # Low on purpose. Choosing a tool is a one-right-answer decision with no
    # creative upside, and a plausible-but-wrong tool at 0.6 is a real failure.
    temperature: float = 0.4
    # Everything one call may generate. Not just the answer: with `thinking` on
    # the reasoning comes out of the same allowance, and a spoken reply of forty
    # words is nothing beside two thousand characters of deliberation. 600 was
    # sized for the answer alone and a hard think ate all of it, stopping mid
    # sentence with nothing left to say - which reached the speakers as "I could
    # not put an answer together". It is a cap rather than a reservation, so
    # generous costs nothing.
    max_tokens: int = 2000
    # Tool calls allowed in one turn before the loop stops and asks for the
    # answer. This costs patience rather than context - somebody is waiting
    # through every one of them - so it is the one cap not set by the token
    # budget. Eight ran out on a real request - look, focus, look, click,
    # look, type, look, check is already eight with nothing having gone
    # wrong - and twelve is only four more than that, which is one mistake
    # and its recovery. Sixteen leaves room to be wrong once and carry on.
    max_steps: int = 16
    # Turns of conversation kept. Cut whole turns rather than messages - half a
    # turn leaves a tool result whose call is gone, which some endpoints reject.
    # 20 rather than 6 because the meter said so: the prompt sits at 2.6k of a
    # 64k window, so six turns was throwing away conversation to save nothing.
    # Trimming is also the one thing that invalidates a cached prefix, since
    # everything after the system prompt shifts - so trimming rarely is faster
    # than trimming often, on top of remembering more.
    history_turns: int = 20
    # Longest wait for one completion. Prompt processing on a local model is the
    # slow part, and a 100k context reprocessing from cold takes most of a minute.
    timeout_seconds: float = 180.0
    # Read the reply as it is generated rather than waiting for all of it. The
    # answer is spoken at the end either way; what this buys is a terminal
    # showing the model think instead of a spinner, which is the difference
    # between waiting and watching. Turn it off for an endpoint whose streaming
    # is unreliable - nothing else depends on it.
    stream: bool = True
    # Context window, for the meter in the corner of the terminal and for the
    # ceiling below. 0 asks the server, which llama.cpp answers on /props; set it
    # for an endpoint that does not, or to correct one that lies.
    context_limit: int = 0
    # Most of the window the conversation may take up. history_turns counts
    # turns and turns are not the same size: a greeting is 50 tokens and a turn
    # that scans a crowded window twice is 6000, so twenty of the second kind
    # would overflow a 64k window and the request would simply fail. Whichever
    # of the two bites first wins. 0 leaves only the turn count.
    max_context_fraction: float = 0.7
    # Where the droppable half of the conversation starts being emptied, as a
    # fraction of the window. Droppable is reasoning and tool results; what
    # stays is what was asked, what was called and what was answered. Both go
    # oldest first, whichever comes first, because a thought and the scan it
    # led to are worth the same nothing an hour later. 0.7 is about 45k of a
    # 64k window. 0 turns it off and leaves dropping whole turns as the only
    # way down.
    squash_fraction: float = 0.7
    # When emptying is not enough, summarise. As a fraction of the ceiling
    # above rather than of the window: at 0.7 and 0.8 it means 36k of a 64k
    # window made up of nothing but prompts, replies and calls, with every
    # result and every thought already gone. The oldest half of that is
    # replaced by one paragraph of what happened, in the model's own words -
    # a story rather than a log, since a target number written down is a lie
    # by the time it is read. Costs one model call, on the turn it happens.
    # 0 turns it off and leaves dropping whole turns as the only way down.
    summarise_fraction: float = 0.8
    # Send one throwaway request at startup, so the system prompt and the tool
    # schemas are already in the server's cache when somebody first speaks. It
    # costs a second or two of nobody's time and takes it off the first answer,
    # which is the one that would otherwise feel broken.
    preload: bool = True
    # The shell tool, which is how anything that is not the desktop gets done -
    # files, git, and a coding agent if one is named below. It runs whatever the
    # model asks for, as you, with no confirmation: that is the point of it and
    # also the reason it is a switch. Everything else works with it off.
    shell: bool = True
    # Let the model reason before answering. On, and the measurement is worth
    # keeping: off, a greeting comes back in 0.4s rather than 2.2s, which is a
    # real difference in a conversation - but with ten tools in front of it the
    # model started writing calls as prose, `search_web(query="...")` in the
    # text where the answer should be. The loop catches that and asks again
    # rather than reading it out, so this is safe to turn off; it costs a round
    # trip when it happens. Sent as chat_template_kwargs, which is what
    # llama.cpp's --jinja passes to the model's own template.
    thinking: bool = True
    # Searching and reading the web. THE ONLY THING IN THE DEFAULT INSTALL THAT
    # LEAVES THIS MACHINE - a query goes to the search engine below and a page
    # request goes to whatever site it names. On because there is no local
    # equivalent: off, the feature simply does not exist. The startup line says
    # so every time, and everything else still works without it.
    web: bool = True
    # DuckDuckGo's HTML endpoint, which needs no key. It is somebody's page
    # rather than somebody's contract, so it can change shape - point this at
    # your own SearXNG for the version that cannot.
    search_url: str = "https://html.duckduckgo.com/html/"
    # Results offered per search. Five is a screenful of context and about as
    # much as is worth reading before picking one to open.
    search_results: int = 5
    # Characters kept from a page - about 1500 tokens per 3000, measured. An
    # article is a few thousand characters of sentences and forty thousand of
    # navigation, and cutting in the middle of the paragraph with the answer in
    # it is the expensive mistake, not the tokens.
    page_chars: int = 6000
    # Offer screenshot() and look_at_image(). Needs a model loaded with a vision
    # projector - llama-server says so on /props as modalities.vision, and
    # startup warns if this is on and that is false. On a model that cannot see,
    # the picture is a couple of thousand tokens of payload for nothing.
    images: bool = True
    # A command line coding agent to hand real code changes to, if you have one -
    # JARVIS is told to run `<this> "the whole request"` rather than editing
    # source a line at a time through the shell. Empty and it is simply told
    # coding is not its job.
    coding_agent: str = ""
    # A command is waited on, so an interactive one has to be killed rather than
    # sat with. Nothing in a voice conversation should take longer than this.
    shell_timeout_seconds: float = 60.0
    # Output kept per command, cut out of the middle. The head says what ran and
    # the tail carries the error, so a prefix loses the half that mattered.
    # About 500 tokens per 1000 characters.
    shell_output_chars: int = 4000
    # What JARVIS writes down for itself with the remember tool, and reads back
    # into its prompt at the start of every turn. How the desk behaves, most of
    # which is only discoverable by getting it wrong, and who it is talking to.
    # None of it is the same on the next machine, so the list is grown rather
    # than shipped. Plain markdown under headings: edit or delete any of it.
    memories: bool = True
    # The file it writes to, under the project root unless it is an absolute
    # path. Not in git - it is about this desk and whoever sits at it. Every
    # other markdown file beside it is read as well and read whole: those are
    # reference, written by hand and bounded by hand, and os-navigation.md is
    # the one that ships. Only this one grows on its own, so only this one is
    # capped.
    memories_file: str = "context/memories/memories.md"
    # Where remember() writes during a session. Kept out of context/memories,
    # because everything under there is read into the front of the prompt where
    # a prompt that never changes is cached once and ridden for free - and this
    # half changes mid turn. It goes at the end instead, where changing it costs
    # the few tokens after it rather than every note the server has made about
    # the conversation, and it is folded into the file above when the room goes
    # quiet. Empty writes straight to the file above, the way it used to.
    session_memories_file: str = "logs/session-memories.md"
    # Once the conversation has gone quiet, JARVIS looks back over everything
    # said since the last time it did and writes down whatever was worth
    # keeping. The only way a lesson outlives the conversation it was learned
    # in without somebody typing it up.
    consolidate: bool = True
    # How long quiet is. It used to run on the end of every turn, which is a
    # second model call on every single answer - most of them about nothing,
    # because most turns teach nothing. A lull costs nobody anything and the one
    # call sees what a run of turns added up to rather than one line out of it.
    settle_seconds: float = 60.0
    # How much of the written file is read back. This is prompt, paid on every
    # single call - the only setting here that is - so it is capped. Going over
    # it drops the whole file rather than quietly losing the top of it, and says
    # so in red at startup. Large on purpose: the point of the cap is to catch a
    # file that has run away, not to ration what JARVIS may know. The reference
    # files beside it are not counted.
    max_memory_chars: int = 20000
    # Who JARVIS is. Empty means context/soul/jarvis.md, which is where the
    # prompt lives - prose, tuned by reading it out loud and changing a word,
    # with no copy in the code to drift from. Character and behaviour only:
    # anything about how the desk works belongs in context/memories, which is
    # read in at the end of it. Missing, the brain does not start and says which
    # file it wanted, because a JARVIS with a stand-in personality and no obvious
    # cause is worse than one that stops.
    system_prompt_file: str = ""


@dataclass(frozen=True)
class Config:
    """Top level configuration."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    log_level: str = "INFO"
    # Everything logs/jarvis.log and its backups may take up between them, in
    # megabytes. Past it the oldest goes. It is the whole budget rather than the
    # size of one file: the log is written in four, so that reaching the limit
    # drops a quarter of the history instead of all of it. Every tool call, every
    # result and every thought goes in here, so a busy afternoon is tens of
    # megabytes and 100 is a few weeks. 0 turns rotation off and lets it grow.
    log_max_mb: int = 100

    @property
    def log_dir(self) -> Path:
        return project_root() / "logs"

    @property
    def config_dir(self) -> Path:
        return project_root() / "config"

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON-friendly mapping. Tuples become lists, paths are not included."""
        return _unwrap(self)

    @classmethod
    def load(cls, path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
        """Build a Config from a config file and the environment.

        With no path, the first of ``CONFIG_FILES`` that exists is used.
        """
        environ = os.environ if environ is None else environ
        found = path if path is not None else find_config_file()

        data: dict[str, Any] = {}
        if found is not None and found.is_file():
            data = read_config_file(found)

        config = _apply(cls(), data)
        return _apply(config, _env_overrides(environ))


# Searched in order; the root files are what earlier versions used.
CONFIG_FILES = (
    "config/jarvis.json",
    "config/jarvis.toml",
    "jarvis.json",
    "jarvis.toml",
)

_SECTIONS = frozenset({"audio", "stt", "tts", "service", "screen", "brain"})

# Where to look, not what to set. Without this JARVIS_CONFIG=x is read as a
# setting called "config" and startup fails on the file it was meant to load.
RESERVED_ENV = frozenset({"JARVIS_HOME", "JARVIS_CONFIG"})


def find_config_file(root: Path | None = None) -> Path | None:
    """First config file that exists, or None. JARVIS_CONFIG overrides the search."""
    if explicit := os.environ.get("JARVIS_CONFIG"):
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    root = root or project_root()
    for relative in CONFIG_FILES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def read_config_file(path: Path) -> dict[str, Any]:
    """Parse a config file by extension."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text) if text.strip() else {}
    return tomllib.loads(text)


def _unwrap(value: Any) -> Any:
    """Dataclasses to dicts, tuples to lists, everything else as it is."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _unwrap(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple | list):
        return [_unwrap(item) for item in value]
    return value


def _env_overrides(environ: dict[str, str]) -> dict[str, Any]:
    """Turn JARVIS_STT_BACKEND=x into {"stt": {"backend": "x"}}."""
    overrides: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith("JARVIS_") or key in RESERVED_ENV:
            continue
        remainder = key[len("JARVIS_") :].lower()
        section, _, option = remainder.partition("_")
        if section in _SECTIONS and option:
            overrides.setdefault(section, {})[option] = value
        else:
            overrides[remainder] = value
    return overrides


def _apply(config: Any, data: dict[str, Any]) -> Any:
    """Recursively overlay a mapping onto a frozen dataclass, coercing types."""
    if not data:
        return config
    known = {f.name: f for f in fields(config)}
    updates: dict[str, Any] = {}
    for key, value in data.items():
        # JSON has no comments, so an underscore-prefixed key is a note.
        if key.startswith("_"):
            continue
        spec = known.get(key)
        if spec is None:
            raise ValueError(f"Unknown config option: {key}")
        current = getattr(config, key)
        if is_dataclass(current) and isinstance(value, dict):
            updates[key] = _apply(current, value)
        else:
            updates[key] = _coerce(value, spec.type, key)
    return replace(config, **updates)


def _coerce(value: Any, annotation: Any, key: str) -> Any:
    """Coerce a TOML or environment value to the field's declared type."""
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")

    if "tuple" in text:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(value)
    if "None" in text and (value is None or value == ""):
        return None
    if "bool" in text:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if "int" in text and "str" not in text:
        return int(value)
    if "float" in text:
        return float(value)
    if "str" in text:
        return str(value)
    raise ValueError(f"Cannot coerce {value!r} for config option {key}")
