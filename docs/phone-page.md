# The phone page

A page on your LAN you can talk to. Hold a button, speak, and it goes through the same
Whisper model and into the same transcript the desktop microphone feeds - so an agent
connected over MCP cannot tell which room you were in.

Off by default. It is the one part of JARVIS meant to be reachable from the network.

```json
{ "web": { "enabled": true } }
```

Then start the service as usual, or force it on for one run:

```powershell
.\jarvis.ps1 serve --web
```

It logs a link per address, token included:

```
Phone page: https://192.168.1.110:8771/?t=Qk3n...
```

Open that on the phone, accept the certificate warning once, tap **Connect**.

## Three things that will stop it working

**The firewall, silently.** Cancelling a Windows firewall prompt writes a *Block* rule, and
an explicit Block beats any Allow, so the page is refused with no message at either end.
Check for them:

```powershell
Get-NetFirewallRule -DisplayName "python.exe" | Where-Object { $_.Direction -eq "Inbound" } |
  Select-Object Name, Action, Profile
```

Anything with `Action: Block` has to go, then allow the port. Elevated:

```powershell
Get-NetFirewallRule -DisplayName "python.exe" | Where-Object { $_.Action -eq "Block" } | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "JARVIS phone page" -Direction Inbound `
  -Protocol TCP -LocalPort 8771 -Action Allow -Profile Private
```

`-Profile Private` on purpose: on a network Windows thinks is Public, leave it shut.

**The certificate warning.** A browser will not open a microphone outside a secure context,
and `http://192.168.x.x` is not one. So a self signed certificate is generated on first run
into `config/`, named for every address this machine answers on - a certificate for
`localhost` would not cover the address the phone actually dials. Phones warn once and
remember. Without a certificate the page still loads and typing still works; only the
microphone is unavailable, and the page says so rather than failing on the first press.

One consequence to know: the certificate is named for the addresses this machine had when it
was generated. If your WiFi address is handed out by DHCP and changes, the certificate no
longer covers it - delete `config/web-*.pem` and restart to have a new one written. A static
lease avoids the whole problem.

**Both halves of the link.** The token is not decoration. Anything that reaches the port can
talk into your transcript and hear the replies. Set `web.token` to keep a stable link across
restarts; leave it blank and a new one is generated each start, which invalidates the old
bookmark on purpose.

## What it does

| | |
| --- | --- |
| **Hold to talk** | Records while held, uploads on release, transcribes, into the transcript |
| **Type instead** | The same, without speaking - useful in a quiet room |
| **Also on desktop** | Sends what you type to `say()` instead, so the room hears it |
| **Speak replies here** | The phone reads out what JARVIS says |

The transcript streams both directions over server sent events, so a second phone or a
desktop browser sees the same conversation. Backgrounding the tab is fine - it catches up
from a cursor on return rather than trusting the connection to have survived.

## Decisions worth knowing

**The phone speaks the reply itself.** SAPI renders to the desktop's speakers, and sending
audio to the phone would mean encoding it, buffering it and keeping it in step. The page
already has the text, so it hands it to the browser's own synthesiser. Cheaper, no latency,
and no audio leaves the machine - which keeps the promise the rest of JARVIS makes. The cost
is that the voice is whatever the phone has, not Hazel.

**It runs inside `jarvis serve`, not beside it.** It needs the transcript and the Whisper
model that are already loaded. A separate process would mean a second copy of the model in
VRAM, which on a 12GB card sharing with an LLM is not affordable.

**Recording is a held button rather than continuous.** Continuous means voice detection in
the browser, and a phone in a pocket streaming a hot microphone over WiFi. Hold to talk is
one gesture and no ambiguity about when you were addressing it.

**Whisper is decoded by PyAV, so the container does not matter.** Android hands over
webm/opus and iOS mp4/aac; both arrive as bytes and go straight to `decode_audio`. Measured
here: a 4.2 second utterance at 36KB transcribed in 0.52s, on the CPU, with the GPU busy.

## When it does not work

- **"Connect" does nothing and the log shows no request.** Firewall, almost certainly. See above.
- **The hold button reports the microphone is unavailable.** Served over HTTP, or the
  certificate was rejected rather than accepted. The address bar will say which.
- **Nothing is transcribed but the upload succeeds.** Held too briefly - under about 1.2KB is
  refused before it is sent. Speak, then release.
- **The reply appears but is not spoken.** iOS will not speak until synthesis has run inside
  a real gesture; that is what tapping **Connect** is for. Reload and tap it.
