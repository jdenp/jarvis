"""Microphone capture.

Capture runs on a background thread into a queue, so nothing is lost while the
agent is thinking. One Recognizer and one Microphone stay open for the session -
reopening per utterance is slow and throws away the calibration.

The stream is cut into phrases here rather than by speech_recognition, so that
background noise cannot hold a phrase open. See PhraseEnd, and vad.py for how a
buffer is judged to be speech at all.

`_run` reads four things off its source - CHUNK, SAMPLE_RATE, SAMPLE_WIDTH and
stream.read - so a source does not have to be a device. RemoteStream below is
the same four members fed by the web app, which is what lets a phone use every
phrase rule written here without a second copy of any of it.
"""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import threading
import time
from collections import deque
from types import TracebackType

import speech_recognition as sr

from .config import AudioConfig
from .vad import SAMPLE_RATE, SAMPLES, build_detector

logger = logging.getLogger("jarvis.microphone")

KEEP_SILENCE = 0.5  # silence kept either side of a phrase

# How long a remote stream may go quiet before it counts as gone. Under it the
# gap is read as silence, which is what a gap in the network sounds like.
REMOTE_IDLE_SECONDS = 3.0

# Longest a remote read waits for a chunk before giving up and calling it
# silence. A quarter of the way into the wait for the next 250ms of audio.
REMOTE_WAIT_SECONDS = 0.25

# How much silence that wait is worth. The phrase splitter counts buffers and
# takes each to be one buffer's worth of time, so handing back a single 32ms
# buffer for every 250ms spent waiting runs its clock eight times slow.
QUIET_BUFFERS = max(1, round(REMOTE_WAIT_SECONDS / (SAMPLES / SAMPLE_RATE)))


class MicrophoneError(RuntimeError):
    """Raised when the input device cannot be opened."""


class PhraseEnd:
    """Decides when a phrase has finished, from a trailing window of buffers.

    speech_recognition needs ``pause_threshold`` of *consecutive* quiet buffers,
    so one keyboard click resets the count and a noisy room holds the phrase open
    until the time limit. This still waits for a whole ``pause_threshold`` of
    quiet, but allows it to be interrupted: the window is widened, and the quiet
    inside it only has to add up. So ``pause_threshold`` keeps meaning what it
    says and the fraction buys noise tolerance rather than spending patience.
    """

    def __init__(
        self, seconds_per_buffer: float, pause_threshold: float, quiet_fraction: float
    ) -> None:
        fraction = min(1.0, max(0.05, quiet_fraction))
        self.needed = max(1, math.ceil(pause_threshold / seconds_per_buffer))
        self.window = max(self.needed, math.ceil(self.needed / fraction))
        self._quiet: deque[bool] = deque(maxlen=self.window)

    def feed(self, quiet: bool) -> bool:
        """Add one buffer. True once the window holds a full pause of quiet."""
        self._quiet.append(quiet)
        return sum(self._quiet) >= self.needed

    @property
    def trailing_quiet(self) -> int:
        """Consecutive quiet buffers at the end, so trimming never cuts speech."""
        count = 0
        for quiet in reversed(self._quiet):
            if not quiet:
                break
            count += 1
        return count


class RemoteStream:
    """Audio arriving over the network, shaped like an open input device.

    Everything `_run` asks of a source, so a phone streaming PCM is one and the
    phrase rules, the mute gate and the trimming all apply to it unchanged. What
    it must not be is a file: there is no end of it to reach, and a read that
    returns nothing stops the capture loop for good.

    So a quiet network reads as silence rather than as the end, which is what it
    sounds like - and it means a phone that walks out of range mid sentence still
    gets the words it managed to send, because the silence ends the phrase the
    ordinary way. Past `idle_seconds` of it the stream goes back to sleep and
    blocks instead, so nothing is fed to Silero on behalf of a phone that is not
    there.

    That silence has to arrive at the rate a device would have produced it, which
    it did not for a while - one 32ms buffer per 250ms of waiting, so a phrase
    that should have ended after a second of quiet needed nine, by which time the
    stream had gone to sleep still holding it. The half sentence then came back
    out the moment the phone said anything else, which from the far end looks
    like a microphone that was off transcribing something anyway.
    """

    CHUNK = SAMPLES
    SAMPLE_RATE = SAMPLE_RATE
    SAMPLE_WIDTH = 2

    def __init__(self, idle_seconds: float = REMOTE_IDLE_SECONDS) -> None:
        self.idle_seconds = idle_seconds
        # _run reads `source.stream`, and there is no second object to be.
        self.stream = self
        self._chunks: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._pending = bytearray()
        self._closed = threading.Event()
        self._live = threading.Event()
        self._last = 0.0

    @property
    def live(self) -> bool:
        """Whether anything is streaming into this right now."""
        return self._live.is_set()

    def write(self, pcm: bytes) -> None:
        """Take a chunk of 16 kHz mono 16-bit audio off the network."""
        if self._closed.is_set() or not pcm:
            return
        self._last = time.monotonic()
        self._live.set()
        try:
            self._chunks.put_nowait(bytes(pcm))
        except queue.Full:
            logger.warning("Remote audio backed up, dropping a chunk.")

    def read(self, frames: int) -> bytes:
        """One buffer, blocking. Silence while a live stream is quiet.

        Closing lets whatever already arrived play out first. A phone that hung
        up mid sentence still said the words, and they are sitting in the queue.
        """
        wanted = frames * self.SAMPLE_WIDTH
        while len(self._pending) < wanted:
            if chunk := self._take():
                self._pending += chunk
            elif self._closed.is_set():
                return b""
            elif self.live and time.monotonic() - self._last <= self.idle_seconds:
                # A wait's worth, not a buffer's. See the note above.
                self._pending += bytes(wanted * QUIET_BUFFERS)
            else:
                self._live.clear()
                self._pending.clear()
        buffer = bytes(self._pending[:wanted])
        del self._pending[:wanted]
        return buffer

    def _take(self) -> bytes:
        """The next chunk, waiting only while one is expected to turn up."""
        try:
            if self._closed.is_set():
                return self._chunks.get_nowait()
            return self._chunks.get(timeout=REMOTE_WAIT_SECONDS if self.live else 1.0)
        except queue.Empty:
            return b""

    def close(self) -> None:
        """Stop for good. A blocked read gives up within the second."""
        self._closed.set()
        self._live.clear()

    def __enter__(self) -> RemoteStream:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class Microphone:
    """Continuous background capture from a single source.

    Usually a device. Given a `source` it is whatever that is, and given a `sink`
    it delivers into somebody else's queue - which is how the web app's audio and
    the room's arrive at the service as one stream of phrases.
    """

    def __init__(
        self,
        config: AudioConfig | None = None,
        source=None,
        sink: queue.Queue | None = None,
    ) -> None:
        self.config = config or AudioConfig()
        self.detector = build_detector(self.config)
        self._recognizer = sr.Recognizer()

        self._source = source
        self._given = source is not None
        self._queue: queue.Queue[sr.AudioData] = (
            sink if sink is not None else queue.Queue(maxsize=16)
        )
        self._muted = threading.Event()
        # Separate from _muted, which the echo gate owns and clears on its own.
        self._paused = threading.Event()
        # And separate again: this one is another source having the floor.
        self._deferred = threading.Event()
        self._deaf_until = 0.0
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def sink(self) -> queue.Queue:
        """The queue phrases are delivered into, to share with another source."""
        return self._queue

    @property
    def energy_threshold(self) -> float:
        """Only meaningful for the energy detector; Silero ignores loudness."""
        return getattr(self.detector, "threshold", 0.0)

    def start(self) -> None:
        """Open the device, calibrate if needed, and begin background capture."""
        if self._thread is not None:
            return
        if not self._given:
            try:
                # 16 kHz explicitly. Left to itself speech_recognition opens at the
                # device default - 44100 on this mic - and Silero silently scores
                # frames that are not the length it thinks they are.
                self._source = sr.Microphone(
                    device_index=self.config.device_index,
                    sample_rate=SAMPLE_RATE,
                    chunk_size=SAMPLES,
                )
            except OSError as exc:  # pragma: no cover - hardware dependent
                raise MicrophoneError(f"Could not open input device: {exc}") from exc
            self._calibrate()

        self._running.set()
        self._thread = threading.Thread(target=self._capture, name="jarvis-capture", daemon=True)
        self._thread.start()

    def _calibrate(self) -> None:
        """Measure ambient noise, if the detector actually uses a threshold."""
        if not self.detector.calibrates:
            return
        if self.config.energy_threshold is not None:
            logger.info("Using fixed energy threshold %.0f.", self.config.energy_threshold)
            return
        assert self._source is not None
        logger.info("Calibrating, please stay quiet for %.1fs.", self.config.calibration_seconds)
        try:
            with self._source as source:
                self._recognizer.adjust_for_ambient_noise(
                    source, duration=self.config.calibration_seconds
                )
        except OSError as exc:  # pragma: no cover - hardware dependent
            raise MicrophoneError(f"Could not read from input device: {exc}") from exc

        self.detector.calibrate(self._recognizer.energy_threshold)

    def _capture(self) -> None:
        """Hold the device open and read it until stopped."""
        assert self._source is not None
        try:
            with self._source as source:
                self._run(source)
        except OSError:  # pragma: no cover - hardware dependent
            logger.exception("Microphone capture stopped.")

    def _run(self, source) -> None:
        """Cut the stream into phrases and queue them.

        Ours rather than speech_recognition's, for the phrase end rule in
        PhraseEnd and because gating per buffer is simpler than working out
        after the fact when a delivered phrase was recorded.
        """
        seconds_per_buffer = source.CHUNK / source.SAMPLE_RATE
        if not self.detector.calibrates and source.SAMPLE_RATE != SAMPLE_RATE:
            logger.warning(
                "Device is at %d Hz, but speech detection needs %d Hz. Set audio.vad to 'energy'.",
                source.SAMPLE_RATE,
                SAMPLE_RATE,
            )
        keep = max(1, math.ceil(KEEP_SILENCE / seconds_per_buffer))
        shortest = max(1, math.ceil(self.config.min_speech_seconds / seconds_per_buffer))
        longest = max(1, math.ceil(self.config.phrase_time_limit / seconds_per_buffer))

        frames: deque[bytes] = deque()
        pause: PhraseEnd | None = None
        recorded = spoken = 0

        while self._running.is_set():
            buffer = source.stream.read(source.CHUNK)
            if not buffer:
                break
            if not self._accepting():
                # A gap in the audio, so any streaming state is now stale.
                frames.clear()
                self.detector.reset()
                pause, recorded, spoken = None, 0, 0
                continue

            speech = self.detector.is_speech(buffer)
            frames.append(buffer)

            if pause is None:
                if len(frames) > keep:
                    frames.popleft()
                if speech:
                    pause = PhraseEnd(
                        seconds_per_buffer,
                        self.config.pause_threshold,
                        self.config.pause_quiet_fraction,
                    )
                continue

            recorded += 1
            spoken += speech
            finished = pause.feed(not speech)
            if not finished and recorded < longest:
                continue

            if finished:
                # Trim the silence it ended on, but only what is actually silent -
                # a word inside the window belongs to this phrase, and the next
                # one starts from scratch, so popping it loses it outright.
                for _ in range(max(0, pause.trailing_quiet - keep)):
                    if not frames:
                        break
                    frames.pop()
            else:
                logger.debug("Phrase hit the %.0fs limit.", self.config.phrase_time_limit)
            if spoken >= shortest:
                self._deliver(frames, source)
            frames.clear()
            pause, recorded, spoken = None, 0, 0

    def _accepting(self) -> bool:
        """Whether audio arriving right now is wanted.

        Checked per buffer, so JARVIS's own voice is dropped as it is recorded,
        and so a pause costs no transcription rather than merely hiding it.
        """
        if self._muted.is_set() or self._paused.is_set() or self._deferred.is_set():
            return False
        return time.monotonic() >= self._deaf_until

    def _deliver(self, frames: deque[bytes], source) -> None:
        audio = sr.AudioData(b"".join(frames), source.SAMPLE_RATE, source.SAMPLE_WIDTH)
        try:
            # Whose it is, because the queue may be shared and draining it is
            # then a question of whose phrases are being thrown away.
            self._queue.put_nowait((self, audio))
        except queue.Full:
            logger.warning("Audio queue full, dropping a phrase.")

    def listen(self, timeout: float | None = None) -> sr.AudioData | None:
        """Pop the next captured phrase, or None if nothing arrived in time.

        Whichever source it came from. That is the whole trick of sharing the
        queue: the service reads one stream of phrases and never learns there
        were two microphones.
        """
        try:
            return self._queue.get(timeout=timeout)[1]
        except queue.Empty:
            return None

    def mute(self) -> None:
        """Stop queueing audio, so JARVIS does not transcribe its own voice."""
        self._muted.set()

    def unmute(self) -> None:
        """Resume queueing, discarding anything captured while JARVIS spoke.

        The guard runs past the moment speech stopped, for the echo tail.
        """
        self._deaf_until = time.monotonic() + self.config.echo_guard_seconds
        self.drain()
        self._muted.clear()

    def defer(self, elsewhere: bool) -> None:
        """Stop capturing while another source has the floor.

        Two live microphones in one room hear the same sentence twice, and the
        second copy of it arrives as a follow-up question nobody asked. Its own
        flag again, because it is neither the echo gate nor a pause anybody
        asked for, and it must not clear either of them on its way out.

        Nothing is drained. The queue is shared with whatever took the floor, so
        emptying it here would throw away the phrase that caused this.
        """
        if elsewhere:
            self._deferred.set()
        else:
            self._deferred.clear()

    def pause(self) -> None:
        """Stop reading the microphone until resumed.

        Its own flag rather than mute(): a reply finishing calls unmute(), and
        that must not undo a pause the user asked for. Queued phrases go too -
        one arriving a second after the key is pressed is the surprise this is
        here to remove.
        """
        self._paused.set()
        self.drain()

    def resume(self) -> None:
        """Start reading the microphone again."""
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def drain(self) -> None:
        """Discard any queued phrases of mine, and put back everyone else's.

        Somebody shutting the desk microphone has not asked for the sentence
        their phone sent a moment ago to be forgotten, and the two share a
        queue.
        """
        held = []
        while True:
            try:
                held.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for waiting in held:
            if waiting[0] is not self:
                with contextlib.suppress(queue.Full):
                    self._queue.put_nowait(waiting)

    def stop(self) -> None:
        """Stop background capture and release the device."""
        self._running.clear()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
            logger.debug("Microphone stopped.")
        if not self._given:
            self._source = None

    def __enter__(self) -> Microphone:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @staticmethod
    def list_devices() -> list[tuple[int, str]]:
        """Return (index, name) for every device that can actually record.

        speech_recognition lists outputs too, and picking one fails at open.
        """
        import pyaudio

        audio = pyaudio.PyAudio()
        try:
            devices = []
            for index in range(audio.get_device_count()):
                info = audio.get_device_info_by_index(index)
                if info.get("maxInputChannels", 0) > 0:
                    devices.append((index, str(info.get("name", "?"))))
            return devices
        finally:
            audio.terminate()
