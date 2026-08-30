"""The page a phone talks to JARVIS through.

One file, no build step and no dependency, the same bargain `ui.py` takes for
the terminal. It is served by the voice service and it is off by default - see
`service.start_webapp`, and put `tailscale serve` in front of it, which is also
what gets you the https a browser insists on before it will open a microphone.

What it sends is raw PCM at 16 kHz, not a recording. That is exactly what
`vad.py` wants, so there is no container to demux and no codec to decode at the
other end - and because it is bytes rather than a file, the seam between one
POST and the next is invisible to the phrase splitter. A quarter second of audio
is 8KB down a socket that is already open.

One AudioContext does both ends, and that is the point rather than tidiness.
iOS suspends a context that goes idle and will not resume one again without a
tap, so a reply arriving after a quiet minute starts a source on a stopped
graph - no sound, no error. A silent loop through it was not enough to hold
the session. A live capture stream is: while the microphone is on, the context
carrying it cannot go to sleep, so playing the reply through that same context
is the one route on a phone that is awake exactly when it is needed.

It runs at the device's own rate for that reason, and the microphone audio is
resampled to 16 kHz in here instead - by averaging rather than by picking every
third sample, which is a low pass filter cheap enough to write out longhand and
the difference between speech and speech with aliasing on it.

The route matters as much as the volume, and it is the thing nobody thinks to
check. A page with a microphone open puts iOS into a recording session, and a
recording session sends its output to the earpiece - so a phone lying on a desk
plays the reply perfectly into the receiver and sounds broken.

The fix is one line and it looks like it does nothing. `navigator.audioSession`
already reports `play-and-record`, and *setting* it to `play-and-record` - the
value it already had - is what moves the output to the loudspeaker. A session
the phone chose and the same session asked for are not the same session. Safari
16.4 and later; everything else ignores the assignment, which is the right
failure, since the phones that need it are the ones that have it.

It is asked for immediately before every clip, not once at the start. Set only
in the unlock it worked perfectly whenever a button had just been pressed and
never when a reply arrived on its own, which reads exactly like audio that does
not work and is in fact audio playing into the earpiece.

Playback goes through the context's own output. An <audio> element fed from a
MediaStreamAudioDestinationNode was tried alongside it while the routing was
still a mystery, and came out again once it was not.

Every wait has a timeout anyway. A queue drained by an await that never returns
is a page that goes quiet for the rest of the session with nothing on screen to
say why, and that was this once already. What none of it can do anything about
is the ring/silent switch, which mutes audio outright - so a refusal says so
rather than being swallowed.

One microphone control here, and it is this phone's. The desk's own key was
mirrored up here for a while as a banner - `desk microphone is off`, with a
button to turn it back on - and it was a control for a room you are not standing
in, sitting above the conversation where it read as though it were about this
phone. The desk has a key on the desk.

Headphone mode is still here, as a checkbox with the other one, because it is a
setting rather than a control: something that is true or false about the desk,
not a button that does anything in this room. It means nothing to what happens
here - what plays on this phone comes out of this phone, and the browser's own
echo cancellation is what keeps that out of the microphone.

The reply arrives the way everything else here does: by long polling, and it is
played here rather than at the desk - a machine talking to an empty room is no
use to somebody in another one. The service renders it to a wav and stops
speaking locally for as long as a page keeps polling.

There is a level meter and a device picker, and neither is decoration. The
failure this page has is silence, and silence looks exactly like a muted
microphone, the wrong input device, and a browser that never granted
permission. The meter separates the first two from a page that is not sending;
the picker fixes the one that actually happens, which is Chrome handing you a
live track from a device nobody is talking into - a Steam or OBS virtual
microphone, on the machine this was written on.

The meter reports and does not judge. It said 'no signal' after a few flat
seconds for a while, and a quiet room set it off, which is exactly the sort of
warning people learn to ignore. The number is the whole of it.

The lock screen buttons work the microphone. iOS gives a page that is playing
audio a media session, and play/pause from the control centre goes to that
session rather than to anything on the page, so those two are handled - the one
control worth having when the phone is in a pocket and the screen is off. An
AirPods stem is not one of them, and does not appear to be reachable from a
page at all.

Being talked over is the same question asked from the other end. The page holds
its audio back while a clip is playing, which is right on a loudspeaker and
exactly wrong on headphones: the interruption went nowhere and the reply talked
over the top of it. On headphones it keeps sending, and a phrase arriving while
a clip runs stops the clip - the same evidence the desk uses, decided here
because the audio is here and the service cannot stop what it is not playing.

The raw switch is the other suspect. Echo cancellation, noise suppression and
gain control are asked for by default because the browser plays the reply a
few inches from the microphone that is listening for the next thing - but that
same processing decides what counts as noise, and on some devices it decides
everything does. Off, the audio is whatever the microphone heard.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#101216">
<title>JARVIS</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #101216; color: #e6e6e6;
    font: 17px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    display: flex; flex-direction: column; height: 100dvh;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) 0 env(safe-area-inset-left);
  }
  header {
    padding: 12px 16px; border-bottom: 1px solid #23262d;
    display: flex; align-items: baseline; gap: 10px;
  }
  h1 { font-size: 15px; letter-spacing: .18em; margin: 0; color: #8a8f98; font-weight: 600; }
  #state { font-size: 13px; color: #6b7280; margin-left: auto; }
  #log {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .line { max-width: 90%; }
  .line .who { font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: #6b7280; }
  .you { align-self: flex-end; text-align: right; }
  .you .what { color: #9fc6ff; }
  .jarvis .what { color: #e6e6e6; }
  footer {
    border-top: 1px solid #23262d; padding: 12px 16px;
    padding-bottom: calc(12px + env(safe-area-inset-bottom));
    display: flex; flex-direction: column; gap: 10px; background: #14171c;
  }
  form { display: flex; gap: 8px; }
  /* The typing box only. It used to be every input, which stretched and padded
     the two checkboxes as well and left them sitting at different indents. */
  #line {
    flex: 1; min-width: 0; background: #1b1f26; border: 1px solid #2b3038; color: inherit;
    border-radius: 10px; padding: 12px 14px; font: inherit;
  }
  button {
    background: #1b1f26; border: 1px solid #2b3038; color: inherit;
    border-radius: 10px; padding: 12px 16px; font: inherit; cursor: pointer;
  }
  button:active { background: #232833; }
  #mic { padding: 16px; font-weight: 600; }
  #mic.on { background: #7f1d1d; border-color: #b91c1c; color: #fff; }
  #meter {
    display: none; height: 6px; border-radius: 3px; background: #1b1f26;
    overflow: hidden;
  }
  #meter.on { display: block; }
  #level { height: 100%; width: 0; background: #3f8f4f; transition: width .1s linear; }
  #sent { font-size: 12px; color: #6b7280; text-align: center; }
  #doing {
    display: none; padding: 8px 16px; font-size: 13px; color: #7f8896;
    font-style: italic; border-top: 1px solid #23262d;
  }
  #doing.on { display: block; }
  #rawline, #phoneline {
    font-size: 13px; color: #8a8f98; display: flex; gap: 8px; align-items: center;
  }
  select {
    background: #1b1f26; border: 1px solid #2b3038; color: inherit;
    border-radius: 10px; padding: 10px 12px; font: inherit; width: 100%;
  }
</style>
</head>
<body>
<header><h1>JARVIS</h1><span id="state">connecting</span></header>
<div id="log"></div>
<div id="doing"></div>
<footer>
  <form id="typing"><input id="line" placeholder="Type instead" autocomplete="off"></form>
  <select id="devices" title="Which microphone"></select>
  <label id="rawline"><input type="checkbox" id="raw">
    Raw audio (no echo cancellation or noise gating)</label>
  <label id="phoneline"><input type="checkbox" id="phones">
    Headphone mode - listen while speaking</label>
  <button id="mic">Mic off</button>
  <div id="meter"><div id="level"></div></div>
  <div id="sent"></div>
</footer>
<script>
const RATE = 16000;
const FLUSH_MS = 250;

const log = document.getElementById('log');
const state = document.getElementById('state');
const phones = document.getElementById('phones');
const mic = document.getElementById('mic');
const meter = document.getElementById('meter');
const level = document.getElementById('level');
const sent = document.getElementById('sent');
const devices = document.getElementById('devices');
const doing = document.getElementById('doing');
const raw = document.getElementById('raw');
raw.checked = localStorage.getItem('raw') === 'yes';
// The loudspeaker. Asking for the session the phone had already chosen is what
// moves the output off the earpiece - see the note at the top of this file.
function setRoute() {
  if (!navigator.audioSession) return;
  try {
    navigator.audioSession.type = 'play-and-record';
  } catch (err) {
    // Older Safari, which had no say in it anyway.
  }
}

function routing() {
  return navigator.audioSession ? navigator.audioSession.type : 'not supported';
}

let bytesSent = 0, loudest = 0, playing = null;

let ctx = null, media = null, node = null, timer = null, wake = null;
let pending = [], streaming = false, broken = '';

function show(who, text) {
  const line = document.createElement('div');
  line.className = 'line ' + (who === 'you' ? 'you' : 'jarvis');
  const label = document.createElement('div');
  label.className = 'who';
  label.textContent = who;
  const what = document.createElement('div');
  what.className = 'what';
  what.textContent = text;
  line.append(label, what);
  log.append(line);
  log.scrollTop = log.scrollHeight;
}

// Long polling, one loop per stream. A failure is a pause and a retry rather
// than an error: a phone loses its network constantly and says nothing about it.
async function follow(path, key, who) {
  let cursor = null;
  for (;;) {
    try {
      const first = cursor === null;
      const url = path + '?since=' + (first ? 0 : cursor) + '&wait=' + (first ? 0 : 25);
      const data = await (await fetch(url)).json();
      const items = first ? data[key].slice(-10) : data[key];
      for (const item of items) {
        show(who, item.text);
        if (key === 'spoken' && !first) play(item.id);
        // Somebody talked over the reply. The same rule the desk uses, and
        // the same evidence: a phrase arriving while a clip is playing was
        // said over the top of it. Only on headphones, where an open
        // microphone through a reply is not the reply coming back.
        if (key === 'heard' && !first && phones.checked) hushPlayback();
      }
      cursor = data.cursor;
      state.textContent = broken || (streaming ? 'listening' : 'connected');
    } catch (err) {
      state.textContent = 'offline';
      await new Promise(done => setTimeout(done, 2000));
    }
  }
}

// One at a time and in order, because two replies over each other is worse
// than the second one arriving late. A 404 is the ordinary case for a line
// that was spoken at the desk instead.
const queued = [];

// The unlock. A context has to be created and resumed inside a tap, and a
// buffer has to actually be scheduled on it - iOS counts the graph having run,
// not the promise having resolved.
//
// The silent loop is not decoration either. iOS suspends a context that goes
// idle, and one that has suspended cannot be resumed again without another
// tap - so a reply arriving after a quiet minute starts a source on a stopped
// graph, which makes no sound and raises nothing. A loop that never ends keeps
// the graph running, which is what everything else on a phone does too.
function unlock() {
  if (!ctx) {
    // No sample rate asked for. The microphone is resampled to 16 kHz on the
    // way out instead, so that the reply plays at whatever this device is
    // actually good at rather than at Whisper's rate.
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    const awake = ctx.createBufferSource();
    awake.buffer = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
    awake.loop = true;
    awake.connect(ctx.destination);
    awake.start(0);
  }
  if (ctx.state !== 'running') ctx.resume();
  setRoute();
}

// Drop what is playing and everything behind it. Called when a phrase lands
// while a clip is running, which is the definition of being talked over.
function hushPlayback() {
  queued.length = 0;
  if (playing) {
    try { playing.stop(); } catch (err) { /* already finished */ }
  }
}

async function play(id) {
  queued.push(id);
  if (playing) return;
  while (queued.length) {
    const next = queued.shift();
    try {
      const reply = await fetch('voice/' + next + '.wav');
      // The ordinary case for a line that was spoken at the desk instead.
      if (!reply.ok) continue;
      if (!ctx) throw new Error('no audio context yet - tap anything once');
      if (ctx.state !== 'running') await ctx.resume();
      // Said out loud rather than played into the void: starting a source on a
      // context that is not running is silence with no error anywhere.
      if (ctx.state !== 'running') {
        throw new Error('audio is ' + ctx.state + ', tap the page once');
      }
      // Every time. iOS drifts back to the earpiece and says nothing about it.
      setRoute();

      const buffer = await ctx.decodeAudioData(await reply.arrayBuffer());
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      playing = source;
      await new Promise((done, fail) => {
        // Its own length plus a margin. Never no limit: the queue behind this
        // is drained by whatever finishes here.
        const timer = setTimeout(() => fail(new Error('it never finished')),
          (buffer.duration + 5) * 1000);
        source.onended = () => { clearTimeout(timer); done(); };
        source.start(0);
      });
    } catch (err) {
      // Never swallowed, and in the conversation rather than in a status line:
      // silence with no explanation is the one failure this page cannot afford
      // twice, and the route is in it because that was the answer once.
      show('jarvis', 'Could not play that here - ' + (err.name || '') + ' '
        + (err.message || err) + ' (route ' + routing() + ')'
        + '. On an iPhone the ring/silent switch mutes'
        + ' this; the volume buttons while it is playing set the right level.');
    } finally {
      playing = null;
    }
  }
}

// What JARVIS is doing, straight off the line the terminal draws. Long polled
// rather than sampled: a status that lags is worse than none, and a phone
// asking every second all day is worse than both.
async function watchDoing() {
  let version = 0;
  for (;;) {
    try {
      const data = await (await fetch('live?since=' + version + '&wait=25')).json();
      version = data.version;
      doing.textContent = data.doing;
      doing.classList.toggle('on', !!data.doing);
    } catch (err) {
      doing.classList.remove('on');
      await new Promise(done => setTimeout(done, 2000));
    }
  }
}

async function status() {
  for (;;) {
    try {
      const data = await (await fetch('status')).json();
      phones.checked = data.headphones;
    } catch (err) { /* follow() is already saying so */ }
    await new Promise(done => setTimeout(done, 4000));
  }
}

document.getElementById('typing').onsubmit = async event => {
  event.preventDefault();
  unlock();
  const box = document.getElementById('line');
  const text = box.value.trim();
  if (!text) return;
  box.value = '';
  await fetch('typed', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: text}),
  });
};

// The desk, and the same Num Lock key held down rather than pressed. It leaves
// the microphone there open while JARVIS talks, so a reply can be cut off mid
// sentence - free on headphones, and a machine transcribing itself on speakers.
// The box is checked already; the next poll of /status confirms it.
phones.onchange = async () => {
  unlock();
  await fetch('headphones', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({on: phones.checked}),
  });
};

// Raw frames straight off the graph. Nothing is written to the output, so
// connecting this to the speaker is silent - it is only there because a node
// nothing pulls from is a node that never runs.
const TAP = `
class Tap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0][0];
    if (channel) this.port.postMessage(new Float32Array(channel));
    return true;
  }
}
registerProcessor('tap', Tap);
`;

function resample(input, from, to) {
  if (from === to) return input;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    // The mean of everything this output sample covers. Picking one of them
    // and throwing the rest away is what puts aliasing into the audio Whisper
    // then has to read.
    const from_ = Math.floor(i * ratio);
    const to_ = Math.min(Math.floor((i + 1) * ratio), input.length);
    let total = 0;
    for (let at = from_; at < to_; at++) total += input[at];
    out[i] = to_ > from_ ? total / (to_ - from_) : input[from_] || 0;
  }
  return out;
}

function flush() {
  if (!pending.length || !ctx) return;
  let total = 0;
  for (const frame of pending) total += frame.length;
  const joined = new Float32Array(total);
  let at = 0;
  for (const frame of pending) { joined.set(frame, at); at += frame.length; }
  pending = [];

  const down = resample(joined, ctx.sampleRate, RATE);
  const pcm = new Int16Array(down.length);
  let peak = 0;
  for (let i = 0; i < down.length; i++) {
    const sample = Math.max(-1, Math.min(1, down[i]));
    if (Math.abs(sample) > peak) peak = Math.abs(sample);
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }

  // Shown whether or not it is sent, so a muted microphone reads as a meter
  // that never moves rather than as a JARVIS that is ignoring you.
  loudest = Math.max(loudest, peak);
  level.style.width = Math.min(100, Math.round(peak * 140)) + '%';

  // Its own voice is not worth transcribing, and the browser's echo
  // cancellation is aimed at the speaker rather than at a file being played.
  // Headphone mode says there is nothing in the room to hear the reply, which
  // is the one case where this hold-back is what stops you interrupting: the
  // words went nowhere and JARVIS talked over the top of them.
  if (playing && !phones.checked) return;

  bytesSent += pcm.byteLength;
  // Two decimals, because the difference between a silent device and a very
  // quiet one is the whole question and both round to zero percent. Reported
  // rather than judged: a quiet room reads the same as a dead microphone from
  // here, and only the person in the room knows which it is.
  sent.textContent = Math.round(bytesSent / 1024) + ' kB sent, loudest '
    + (loudest * 100).toFixed(2) + '%';
  fetch('audio', {
    method: 'POST',
    headers: {'Content-Type': 'application/octet-stream'},
    body: pcm.buffer,
  }).catch(() => { state.textContent = 'offline'; });
}

// Labels are empty until something has been granted, so this is worth calling
// again once a stream is open - before that the list is a row of blanks.
async function listDevices() {
  let found = [];
  try {
    found = (await navigator.mediaDevices.enumerateDevices())
      .filter(d => d.kind === 'audioinput');
  } catch (err) { return; }
  const chosen = devices.value || localStorage.getItem('mic') || '';
  devices.innerHTML = '';
  for (const found_ of [{deviceId: '', label: 'Default microphone'}, ...found]) {
    if (found_.deviceId === 'default' || found_.deviceId === 'communications') continue;
    const option = document.createElement('option');
    option.value = found_.deviceId;
    option.textContent = found_.label || 'Microphone ' + found_.deviceId.slice(0, 6);
    devices.append(option);
  }
  devices.value = chosen;
}

raw.onchange = () => {
  localStorage.setItem('raw', raw.checked ? 'yes' : 'no');
  devices.onchange();
};

devices.onchange = async () => {
  localStorage.setItem('mic', devices.value);
  if (!streaming) return;
  // Swap the device under a running stream by restarting it, which is the
  // only way: a track's source cannot be changed once it is open.
  await hush();
  loudest = 0;
  await listen();
};

async function listen() {
  const wanted = devices.value;
  const processing = !raw.checked;
  media = await navigator.mediaDevices.getUserMedia({
    audio: Object.assign(
      {
        channelCount: 1,
        echoCancellation: processing,
        noiseSuppression: processing,
        autoGainControl: processing,
      },
      wanted ? {deviceId: {exact: wanted}} : {},
    ),
  });
  await listDevices();
  const track = media.getAudioTracks()[0];
  show('jarvis', 'Listening through ' + (track ? track.label : 'nothing') + '.');
  // The same context playback uses, so that a live microphone is what keeps it
  // from going to sleep between replies.
  unlock();
  await ctx.resume();
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([TAP], {type: 'text/javascript'})));
  node = new AudioWorkletNode(ctx, 'tap');
  node.port.onmessage = event => pending.push(event.data);
  ctx.createMediaStreamSource(media).connect(node).connect(ctx.destination);
  timer = setInterval(flush, FLUSH_MS);

  // Kept awake, because a suspended graph is a stream that stops. In practice a
  // phone with its microphone open keeps running with the screen off anyway -
  // the capture session is what holds it - which is why going to the background
  // no longer hands the desk back.
  try { wake = await navigator.wakeLock.request('screen'); } catch (err) { wake = null; }
}

async function hush() {
  if (timer) { clearInterval(timer); timer = null; }
  if (node) { node.port.onmessage = null; node.disconnect(); node = null; }
  if (media) { media.getTracks().forEach(track => track.stop()); media = null; }
  // The context stays. Closing it would take playback down with the
  // microphone, and it cannot be opened again without another tap.
  if (wake) { try { await wake.release(); } catch (err) {} wake = null; }
  pending = [];
}

// Said to the service as well as done here. It is holding whatever arrived
// before the button was pressed, and half a sentence should not be waiting to
// come out when the microphone is opened again.
function tellMic(on) {
  return fetch('microphone', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({on: on}),
  }).catch(() => {});
}

async function stopMic() {
  streaming = false;
  mic.classList.remove('on');
  meter.classList.remove('on');
  mic.textContent = 'Mic off';
  state.textContent = 'connected';
  nowPlaying(false);
  await hush();
  // After the local stop, so anything still in flight lands before the drain.
  await tellMic(false);
}

async function startMic() {
  await tellMic(true);
  try {
    await listen();
  } catch (err) {
    broken = 'no microphone: ' + err.name;
    state.textContent = broken;
    show('jarvis', 'The browser would not open the microphone. ' + err.name
      + ': ' + err.message);
    await hush();
    return;
  }
  streaming = true;
  mic.classList.add('on');
  meter.classList.add('on');
  mic.textContent = 'Mic on';
  state.textContent = 'listening';
  nowPlaying(true);
}

// The lock screen. A page playing audio shows up on iOS as a media session, and
// play/pause from the control centre goes to that session rather than to
// anything on the page - so the one control worth reaching without looking at a
// screen is the microphone.
//
// It was doing something already: pausing from the lock screen suspended the
// audio graph, which stopped the capture as a side effect. This makes that the
// intent rather than a side effect, and gives the play half something to do.
// Not the AirPods stem, which was tried and does not appear to be reachable
// from a page at all - see DESIGN.
function nowPlaying(on) {
  const session = navigator.mediaSession;
  if (!session) return;
  session.playbackState = on ? 'playing' : 'paused';
  try {
    // The name and nothing else. It said whether the microphone was on for a
    // while, which is a second copy of a state the page already draws and one
    // that iOS is free to leave on screen after it stops being true.
    session.metadata = new MediaMetadata({title: 'JARVIS'});
  } catch (err) {
    // No MediaMetadata here, which costs the title and nothing else.
  }
}

function mediaKeys() {
  const session = navigator.mediaSession;
  if (session) {
    try {
      session.setActionHandler('pause', () => { if (streaming) stopMic(); });
      session.setActionHandler('play', () => { unlock(); if (!streaming) startMic(); });
    } catch (err) {
      // An action the browser will not hand over. Nothing is worse than before.
    }
  }
  nowPlaying(false);
}

mic.onclick = () => {
  unlock();
  return streaming ? stopMic() : startMic();
};

// A page on its way out hands the desk back, and a beacon is the only thing a
// closing page can be relied on to finish. Closing only: this used to fire on
// backgrounding as well, so locking the screen handed the desk back and
// unlocking took it again, which is two announcements for a phone that never
// stopped listening.
function goodbye() {
  try {
    navigator.sendBeacon('gone');
  } catch (err) {
    fetch('gone', {method: 'POST', keepalive: true}).catch(() => {});
  }
}

addEventListener('pagehide', goodbye);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    state.textContent = streaming ? 'paused by the phone' : 'in the background';
  } else {
    // Coming back is a good moment to find the graph stopped.
    if (ctx && ctx.state !== 'running') ctx.resume();
    if (streaming) state.textContent = 'listening';
  }
});

listDevices();
mediaKeys();
navigator.mediaDevices.addEventListener('devicechange', listDevices);
watchDoing();
follow('heard', 'heard', 'you');
follow('spoken', 'spoken', 'jarvis');
status();
</script>
</body>
</html>
"""


def page() -> bytes:
    """The whole app, ready to write down a socket."""
    return PAGE.encode("utf-8")
