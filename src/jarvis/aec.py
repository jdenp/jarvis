"""Cancelling this machine's own sound out of its own microphone.

The speakers and the microphone are in one room, so everything played here comes
back in: JARVIS's own voice, a video, whatever is on. The room is the only part
of that we cannot know in advance. The sound itself we have exactly, because this
machine is the one playing it - WASAPI hands back the mix going to a render
endpoint, and WebRTC's AEC3 learns the path from that endpoint to this microphone
and subtracts what it predicts.

Measured through this pipeline on the desk it was written for, 24 dB: a video
playing takes Silero from hearing speech in 315 buffers out of 437 to nought,
and Whisper from a paragraph of it to an empty string. It costs 0.6% of one core
and no VRAM, and adds 9ms to the capture path.

What gets transcribed is the cancelled audio, not what the microphone heard. It
was the other way round for an afternoon, because cancelling costs about 20
points of word error on the person talking over the speakers and the raw audio
reads much better. But the raw audio still has the video in it, and Whisper
wrote some of it down - and a JARVIS that occasionally answers a YouTube video
is worse than one that occasionally mishears you. Muddled is recoverable; out
of nowhere is not.

Noise suppression is on and automatic gain is not, both measured rather than
guessed. Suppression at level 1 halves the word error on a voice two and a half
times louder than the speakers, 28% against 66%. Gain control undoes the whole
point: it lifts the residual echo 5.6x, back to where Whisper transcribed a
"Thank you." out of an empty room.

Optional and quiet about it. Without `uv sync --extra echo` there is nothing to
import, `canceller()` returns None, and capture carries on exactly as before.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import numpy as np

from .config import AudioConfig

logger = logging.getLogger("jarvis.aec")

# AEC3 works in 10ms frames and will not take anything else.
FRAME_MS = 10

# How hard the noise suppressor leans on what the canceller left behind. 1 of 3,
# because 2 and 3 measured no better on a voice at conversational level and
# turned a quiet one into Whisper's favourite hallucination.
SUPPRESSION = 1

# Reference level, as rms, that counts as the speakers being on. Low, because
# the point is to know whether anything is playing at all.
PLAYING = 3e-4

# Seconds of residual kept to find the echo floor in. Long enough to contain a
# gap between words, because the quietest moment in it is the estimate.
FLOOR_SECONDS = 2.0

# Longest a frame will wait for the reference to catch up. Both streams run in
# real time, so what this covers is jitter between two callbacks, not latency.
#
# Substituting silence instead is the bug this exists to prevent, and it was
# expensive: every fabricated sample left the reference one sample further
# behind the microphone, for good, until nothing lined up at all. Measured on
# the real thing, 1.2 dB of cancellation against 42.8 for the same audio paired
# properly. It is the whole difference between this working and not.
CATCH_UP_SECONDS = 0.05

# No block for this long means nothing is playing. WASAPI loopback delivers no
# callbacks at all on an idle endpoint rather than delivering silence, so this
# is how silence is told from a stall - and when it starts again the backlog is
# dropped, because audio from before the gap is no use for what is playing now.
IDLE_SECONDS = 0.15

# How much reference audio is held back before it is handed over, which is the
# single most important number in this file. `take` gives out the oldest it
# holds, so this is how far the reference is delayed - and the reference has to
# land inside AEC3's filter, which only reaches back about 100ms.
#
# Measured on one desk, against a real video at full volume: at 0.40 the
# reference ran 219ms BEHIND the microphone and cancellation was 1.2 dB. At 0.04
# it ran 138ms ahead and was no better. At 0.12 it ran 64ms ahead - which is
# about the real acoustic delay - and cancellation was 33.8 dB. It is a straight
# line: every 10ms of backlog moves the reference 10ms later.
#
# The right value depends on this machine's capture latencies, so it is in
# config as `audio.echo_reference_delay` rather than only here.
DEFAULT_BACKLOG = 0.12

# Reference frames of pure silence before the canceller is told to expect none.
# Nothing playing is the ordinary case, and a filter fed only silence is a filter
# that has forgotten the room by the time something plays.
SILENT_FRAMES = 500


def available() -> str:
    """Empty if this can run, otherwise why it cannot."""
    try:
        import pyaudiowpatch  # noqa: F401
        import pywebrtc_audio  # noqa: F401
    except ImportError as exc:
        return f"{exc.name} is not installed - `uv sync --extra echo`"
    return ""


class Reference:
    """The mix going to the speakers, at the microphone's sample rate.

    Its own capture stream and its own thread, because it has to be sampled
    while the microphone is being sampled rather than asked for afterwards.
    """

    def __init__(self, rate: int, delay: float = DEFAULT_BACKLOG) -> None:
        import pyaudiowpatch as pa

        self.rate = rate
        self.delay = max(0.0, delay)
        self._pa = pa.PyAudio()
        wasapi = self._pa.get_host_api_info_by_type(pa.paWASAPI)
        speakers = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        self._device = next(
            (
                found
                for found in self._pa.get_loopback_device_info_generator()
                if speakers["name"] in found["name"]
            ),
            None,
        )
        if self._device is None:
            self._pa.terminate()
            raise RuntimeError(f"no loopback capture for {speakers['name']!r}")

        self._channels = int(self._device["maxInputChannels"])
        self._device_rate = int(self._device["defaultSampleRate"])
        self._held: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._arriving = threading.Condition(self._lock)
        self._spare = np.zeros(0, np.float32)
        self._last = 0.0
        self._stream = self._pa.open(
            format=pa.paFloat32,
            channels=self._channels,
            rate=self._device_rate,
            input=True,
            input_device_index=self._device["index"],
            frames_per_buffer=self._device_rate // 100,
            stream_callback=self._arrived,
        )
        logger.info("Cancelling against %s.", self._device["name"])

    @property
    def name(self) -> str:
        return str(self._device["name"])

    def _arrived(self, data, frames, info, status):  # pragma: no cover - audio thread
        block = np.frombuffer(data, dtype=np.float32)
        if self._channels > 1:
            block = block.reshape(-1, self._channels).mean(axis=1)
        now = time.monotonic()
        with self._arriving:
            if now - self._last > IDLE_SECONDS:
                # The speakers have just started. Whatever is held is from
                # before the gap and lines up with nothing.
                self._held.clear()
                self._spare = np.zeros(0, np.float32)
            self._last = now
            self._held.append(self._resampled(block))
            self._arriving.notify_all()
        return (None, 0)

    def _resampled(self, block: np.ndarray) -> np.ndarray:
        """To the microphone's rate.

        Averaged down rather than sampled down when the rates divide, which is
        the ordinary case - 48k speakers, 16k microphone. Plain interpolation
        folds everything above 8 kHz back down into the reference as noise that
        was never in the room, and a reference that does not match what the
        microphone heard is a filter that cannot fit it. Measured: it cost a
        third of the cancellation.
        """
        if self._device_rate == self.rate:
            return block.astype(np.float32)
        ratio = self._device_rate / self.rate
        if ratio == int(ratio):
            step = int(ratio)
            usable = len(block) // step * step
            return block[:usable].reshape(-1, step).mean(axis=1).astype(np.float32)
        wanted = round(len(block) / ratio)
        return np.interp(
            np.linspace(0, len(block), wanted, endpoint=False),
            np.arange(len(block)),
            block,
        ).astype(np.float32)

    def take(self, count: int) -> np.ndarray:
        """The next `count` samples the speakers were given.

        Waits for them rather than making them up. Silence only when the
        endpoint really is idle, which is the one case where silence is the
        truth - see CATCH_UP_SECONDS.
        """
        with self._arriving:
            deadline = time.monotonic() + CATCH_UP_SECONDS
            while len(self._spare) + sum(len(b) for b in self._held) < count:
                if time.monotonic() - self._last > IDLE_SECONDS:
                    break  # nothing is playing, and silence is correct
                if not self._arriving.wait(max(0.0, deadline - time.monotonic())):
                    break
                if time.monotonic() > deadline:
                    break
            held = sum(len(block) for block in self._held) + len(self._spare)
            over = held - int(self.delay * self.rate)
            while over > 0 and self._held:
                # Drift, or the microphone stalled. Lose the oldest rather than
                # cancel against something that happened half a second ago.
                oldest = self._held.popleft()
                over -= len(oldest)
            parts = [self._spare]
            have = len(self._spare)
            while have < count and self._held:
                block = self._held.popleft()
                parts.append(block)
                have += len(block)
            joined = np.concatenate(parts) if len(parts) > 1 else self._spare
            self._spare = joined[count:]
            out = joined[:count]
        if len(out) < count:
            out = np.concatenate([out, np.zeros(count - len(out), np.float32)])
        return out

    def stop(self) -> None:
        with self._lock:
            self._held.clear()
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


class Canceller:
    """One microphone's worth of echo cancellation, buffer in and buffer out.

    Takes and returns the same PCM the capture loop already deals in, and the
    same number of samples, so nothing downstream has to know it happened. The
    9ms AEC3 adds and the part-frame left over come out of a small backlog held
    here rather than out of the phrase timing.
    """

    def __init__(self, reference: Reference, rate: int, margin: float = 0.0) -> None:
        import pywebrtc_audio

        self.reference = reference
        self.frame = rate * FRAME_MS // 1000
        self._aec = pywebrtc_audio.AudioProcessor(
            rate,
            1,
            echo_cancellation=True,
            noise_suppression=True,
            ns_level=SUPPRESSION,
        )
        self._waiting = np.zeros(0, np.float32)
        self._done = np.zeros(0, np.float32)
        self._silent = 0
        self.margin = margin
        self._floor: deque[float] = deque(maxlen=max(1, int(FLOOR_SECONDS * 1000 / FRAME_MS)))

    def clean(self, buffer: bytes) -> bytes:
        """One buffer of int16 PCM with this machine's own noise taken out."""
        near = np.frombuffer(buffer, dtype="<i2").astype(np.float32) / 32768.0
        self._waiting = np.concatenate([self._waiting, near])

        while len(self._waiting) >= self.frame:
            frame, self._waiting = self._waiting[: self.frame], self._waiting[self.frame :]
            far = self.reference.take(self.frame)
            self._silent = 0 if far.any() else self._silent + 1
            done = np.asarray(self._aec.process(frame, far), dtype=np.float32).copy()
            self._done = np.concatenate([self._done, self._gated(done, far)])

        if len(self._done) < len(near):
            # Only while it fills, which is the first buffer of a session.
            self._done = np.concatenate(
                [np.zeros(len(near) - len(self._done), np.float32), self._done]
            )
        out, self._done = self._done[: len(near)], self._done[len(near) :]
        return (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def _gated(self, done: np.ndarray, far: np.ndarray) -> np.ndarray:
        """Silence, when what is left is not loud enough to be a person.

        Cancelling is a subtraction and subtraction cannot touch what the
        speakers added themselves: they distort when they are loud, and the
        harmonics they invent were never in the reference. Measured on one desk,
        suppression ran anywhere from 3 dB to 24 dB depending on volume and how
        long the filter had been converging - so leaning on it alone to keep a
        video out of the transcript was always going to leak.

        This is a decision rather than a subtraction, which is why distortion
        cannot defeat it. While the speakers are on, the quietest residual of the
        last couple of seconds is taken as the echo floor, and anything not
        clearly above it is treated as more echo. A person talking is well above
        it; leftover video is by definition at it.
        """
        if self.margin <= 0:
            return done
        loud = float(np.sqrt(np.mean(far**2)))
        if loud < PLAYING:
            # Nothing playing, so there is no echo to be confused with and no
            # floor worth trusting once something starts.
            self._floor.clear()
            return done
        level = float(np.sqrt(np.mean(done**2)))
        self._floor.append(level)
        floor = min(self._floor)
        return done if level > floor * self.margin else np.zeros_like(done)

    @property
    def idle(self) -> bool:
        """Whether the speakers have been silent long enough to say so."""
        return self._silent >= SILENT_FRAMES

    def stop(self) -> None:
        self.reference.stop()


def canceller(config: AudioConfig, rate: int, width: int) -> Canceller | None:
    """One for this microphone, or None with a reason in the log.

    Never raises. Every way this can fail leaves a JARVIS that hears its own
    speakers, which is where it started, and that is a far better outcome than
    one that will not open the microphone at all.
    """
    if not config.echo_cancellation:
        return None
    if width != 2:
        logger.warning(
            "Echo cancellation needs 16-bit audio, but this device is %d-bit.", width * 8
        )
        return None
    if why := available():
        logger.warning("Echo cancellation is on but %s.", why)
        return None
    try:
        return Canceller(
            Reference(rate, config.echo_reference_delay), rate, config.echo_gate_margin
        )
    except Exception as exc:
        logger.warning("Echo cancellation could not start (%s); carrying on without it.", exc)
        return None
