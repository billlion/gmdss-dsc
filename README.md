# gmdss-dsc

A small, readable Python starting point for MF/HF Digital Selective Calling.
The first milestone is to reproduce the message in the public **GMDSS_1**
recording, synthesize a clean WAV, and decode both recordings to the same
symbols and MMSIs. That milestone works.

Implemented independently in Python, with ITU-R M.493-16 as the protocol
reference and SDRangel's DSC receiver as an implementation cross-check.
SDRangel is not a runtime dependency. NumPy and SciPy provide the audio tools.
Released under GPL-3.0-or-later; see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Quick start

Python 3.10 or later:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[gps]"
python -m unittest discover -s tests -v
dsc decode examples/gmdss1_clean.wav --centre 975
```

Generate the example yourself:

```sh
dsc encode-test example.wav --source 215322000 --destination 004634060 --centre 975
dsc decode example.wav
```

`python -m gmdss_dsc` also works in place of `dsc`. The decoder writes JSON;
its exit status is 0 if it finds a complete valid frame, 1 if none, and 2
for an input error. A normal encode uses 1700 Hz unless `--centre` is set.
The checked-in [example WAV](examples/gmdss1_clean.wav) uses 975 Hz to sound
similar to the received sample. Its tones are 890 and 1060 Hz. It is a new,
clean synthesis of the same message, not a reproduction of the receiver's
noise, fading, phase or MP3 compression.

## Local browser interface

NiceGUI includes the web server (FastAPI/Uvicorn) and serves its browser assets
locally. No separate web server, Android app or Internet connection is needed
after installation. The Python computer handles audio and decoding; a phone
or tablet on the same LAN displays messages and controls that shared receiver.

From the project directory, in the same environment used for the CLI:

```sh
python -m pip install -e ".[web,gps]"
dsc serve
```

Open <http://127.0.0.1:8080> on that computer. The optional `web` extra includes
sounddevice; `gps` is only needed for serial NMEA. Linux may also need the
`libportaudio2` system package, as for `dsc listen`. Windows and macOS use
the same Python commands.

For access from the boat's Wi-Fi:

```sh
dsc serve --host 0.0.0.0 --port 8080
```

Use the computer's actual LAN address in the browser, for example
`http://192.168.1.20:8080`. `0.0.0.0` is the listening address, not an
address to enter on the phone. Binding to the computer's fixed LAN IP instead
of `0.0.0.0` also works. Allow TCP port 8080 through its firewall on the
private network; Wi-Fi client isolation must permit devices to communicate.

LAN binding generates a password and prints it once in the server terminal;
the browser login username is **dsc**. To keep a password across restarts,
set `DSC_WEB_PASSWORD` in the server environment before launching:

```sh
# Linux/macOS shell
export DSC_WEB_PASSWORD='your-chosen-password'
dsc serve --host 0.0.0.0
```

```powershell
# Windows PowerShell
$env:DSC_WEB_PASSWORD = 'your-chosen-password'
dsc serve --host 0.0.0.0
```

The environment password is also enforced for loopback access if set.
HTTP Basic authentication covers the page, downloads and WebSocket connection.
This is for a trusted local network: HTTP does not encrypt the password or
messages. Do not forward the port to the Internet.

The first version provides:

- Input device, sample rate, channel and audio-centre settings, and Start/Stop.
  Use **List audio devices** for IDs/names on the server computer. Audio never
  comes from the phone microphone.
- A shared list of complete ECC-passing messages, full JSON details and JSONL
  download. Up to 200 messages and 100 activity entries are retained in memory;
  download the messages before stopping the server to preserve them.
- One-shot position reads from manual coordinates, serial NMEA, gpsd or
  Signal K. The **Position source** panel configures them; fix age and stale
  status keep updating after a read. **Read position** fetches another fix.
  These are the onboard computer's sources, not the phone's location.
- Multiple simultaneous browsers. Reception starts only when requested and
  continues if all browsers close, sleep or disconnect. Reopening the page
  shows the current receiver and retained history. The computer/server must
  stay awake; Ctrl+C in its terminal stops the server and receiver.

Position options can also initialise the form at launch, for example:

```sh
dsc serve --host 0.0.0.0 --position-source signalk --signalk-url http://192.168.1.30:3000
# Or: --position-source gpsd --gpsd-host 192.168.1.30
# Or: --position-source serial --serial-port COM3 --baud 4800
```

For an authenticated Signal K server, set `DSC_SIGNALK_TOKEN` in the server
environment, as for the position CLI. Settings are held in memory and are not
saved across server restarts. This first web interface is for reception and
position checks; the existing encoding/playback commands remain in the CLI.

The UI uses the same decoder and JSON fields as `dsc listen`, adding a message
`id` and `receiver_session` counter so capture segments from different
Start/Stop sessions can be distinguished. It does not open a second soundcard
stream for each browser. A decode already in progress finishes before Stop
closes the receiver; status shows **Stopping** while it completes.

Developer browser check (uses the checked-in WAV, no soundcard):

```sh
python -m pip install playwright
python -m playwright install chromium
python tests/browser_smoke.py
```

## First reference call

| Field | Value |
|---|---|
| Format | Individual, symbol 120 |
| Destination MMSI | `004634060` |
| Category | Safety, symbol 108 |
| Source MMSI | `215322000` |
| First telecommand | Test, symbol 118 |
| Second telecommand | No information, symbol 126 |
| Six data symbols | 126 (no information) |
| End of sequence | Acknowledgement requested, symbol 117 |
| Error-check character | 85 |

The public [PhosphorDSC test page](https://fractaldoctor.github.io/PhosphorDSC/tests.html)
provides an independent decode of the
[Signal Identification Wiki sample](https://www.sigidwiki.com/wiki/GMDSS_Digital_Selective_Calling).
The literal symbol vector and ECC are tested separately from round trips.
The original sample has also been decoded locally with this implementation:
one complete call, the fields above, and no character recovered from a bad
copy. See [reference instructions](examples/REFERENCE.md) to repeat that test.
The original third-party recording is not bundled in this repository.

## Live soundcard input (Windows, Linux and macOS)

The optional [sounddevice](https://python-sounddevice.readthedocs.io/) input accepts **demodulated SSB audio** from a
radio's audio interface, USB soundcard or suitably routed virtual input.
It prints one complete JSON object per decoded call, on its own line.

From the extracted/cloned project directory (the one containing
`pyproject.toml`), using the same Python environment as the WAV decoder:

```sh
python -m pip install -e ".[audio]"
python -m gmdss_dsc devices
python -m gmdss_dsc listen --device 3 --centre 1700
```

Replace `3` with an **input** device ID from the list. `dsc devices` and
`dsc listen` are equivalent shorter commands. Stop with **Ctrl+C**.
Windows/macOS pip installs include PortAudio. Debian/Ubuntu may first need
`sudo apt install libportaudio2`. The offline commands do not require audio
hardware, sounddevice or PortAudio.
Live capture also needs a working OS audio session; on Linux, an audio
server connection error can mean that PulseAudio/PipeWire is not running or
is inaccessible from the current login session.

The default capture rate is 48000 Hz, channel 1 (left/mono). Use
`--sample-rate 44100` or `--channel 2` if needed. `--centre` is the **audio
centre in Hz**, not the RF frequency: 1700 Hz matches 2187.5 kHz DSC received
with a USB dial setting of 2185.8 kHz. Use `--centre auto` to estimate it.
The FUNcube's raw stereo I/Q needs a separate SDR processing stage first.

Standard output contains only newline-delimited JSON during listening;
startup, gap and stop messages go to standard error. For a log file:

```sh
python -m gmdss_dsc listen --device 3 --centre 1700 > calls.jsonl
```

On Windows, use Command Prompt or PowerShell 7.4+ for a UTF-8 redirected
log; older Windows PowerShell may re-encode native-command output.

The JSON retains the existing WAV decoder fields and adds:

| Field | Meaning |
|---|---|
| `capture_segment` | Starts at 0; increases after a capture gap. |
| `data_start_s`, `end_s` | Sample-based times from the start of this uninterrupted segment. The start is the first format character, after phasing. |
| `decoded_at_utc` | Computer UTC time when the result was emitted, **not** the transmitter's time or the exact reception time. |

The listener checks every **1 second**, retaining **30 seconds** of audio.
Calls spanning decode intervals are recovered from overlapping windows.
Repeated decodes of the same burst are suppressed; a later identical call
is still logged. Initial decoding does not wait for the whole 30-second
window to fill. Output follows a complete, valid closing sequence, usually
within the next interval plus processing/buffering time.

`--window 60` retains a longer history; `--interval 2` reduces processing
load. The window must exceed the interval. For reliable boundary coverage,
the window minus the interval should exceed the duration of the complete
call, including its phasing. This wrapper retains the offline modem's
fixed-clock limitations; a longer window does not add clock tracking.

Capture uses a bounded queue so slow decoding cannot grow memory without
limit. On queue loss or a reported hardware overflow, the listener reports
the gap, discards incomplete audio, and starts a new segment. The suspect
overflow block is discarded as well. Keep the soundcard level below clipping
and disable speech noise suppression/enhancements. Select the radio's input
explicitly rather than relying on the default laptop microphone.

Tests replay synthesized DSC audio in irregular chunks through the real
decoder and exercise overlap, repeated transmissions, gap handling,
channel selection and JSON output without requiring audio hardware.
Actual USB-radio capture still needs testing on the user's equipment.

## CW identification, position and soundcard output

Install the optional audio and serial-GPS dependencies on Windows, Linux or macOS:

```sh
python -m pip install -e ".[audio,gps]"
dsc devices
dsc ports
```

`devices` lists audio inputs **and outputs**; `ports` lists the OS serial-port
names. A USB GPS which presents NMEA as a serial port uses the same Python
interface on all three platforms, after any device driver is installed.
gpsd and Signal K clients need no extra Python dependencies.

Both `encode-test` and the new `encode-position` accept `--callsign` to prepend
`DE CALLSIGN` in Morse. Add `--locator IO70XX` for an explicit four- or
six-character Maidenhead locator; on `encode-position`, `--locator auto`
derives a six-character locator from the same position snapshot as the DSC
message. `--cw-wpm 18` is the default. Without `--callsign`, there is no CW.
The CW tone is at `--centre`, followed by a pause and the complete original
DSC synchronization sequence. The radio can stay in USB throughout.

### Check the position source first

These commands print one JSON position without producing audio:

```sh
dsc position --latitude 50.2 --longitude -4.5
dsc position --position-source serial --serial-port COM3 --baud 4800
dsc position --position-source gpsd --gpsd-host 192.168.1.10
dsc position --position-source signalk --signalk-url http://192.168.1.10:3000
```

For serial NMEA, replace `COM3` with the device reported by `dsc ports`:

| Platform | Typical serial-port name |
|---|---|
| Windows | `COM3` |
| Linux | `/dev/ttyUSB0`, `/dev/ttyACM0`, or a stable `/dev/serial/by-id/...` path |
| macOS | `/dev/cu.usbserial-...` |

The same position options apply to `encode-position`. This first version
uses explicit command-line configuration, with no OS-specific configuration
file and no automatic source discovery/fallback. Commands can be saved in
a local shell script or Windows batch file.

| Source | Behaviour |
|---|---|
| `manual` (default) | Requires `--latitude` and `--longitude` in decimal degrees. North/east are positive. The supplied coordinates are treated as current unless `--position-time` supplies an ISO timestamp with timezone. |
| `serial` | Reads checksummed RMC or GGA sentences at `--baud` (default 4800); rejects invalid fixes. RMC supplies a full UTC date/time. GGA has no date: the nearest UTC date is inferred from the computer clock and reported as `serial:GGA:date-inferred`. |
| `gpsd` | Connects to `--gpsd-host` (default localhost), `--gpsd-port` (2947). Accepts a complete protocol-v3 TPV report with a 2D/3D fix, coordinates and UTC time. Partial reports are skipped, never combined across fixes or devices. The first usable device report wins. |
| `signalk` | Reads `vessels/self/navigation/position` through REST at `--signalk-url`. Requires coordinates and the value's own timestamp; rejects stale cached values. The server must supply a trustworthy own-vessel position: this client does not independently query GNSS fix-status paths. |

Each invocation reads **one snapshot**, with `--position-timeout 10` seconds
and `--max-fix-age 30` seconds by default. Old, missing and implausibly future
timestamps fail visibly; no cached coordinates are silently substituted.
Keep the computer clock correct. GGA's missing date means it cannot detect
a recording replayed exactly a day later; use RMC when possible. The raw fix,
source, age and locator appear in JSON. A serial port already owned by gpsd
or Signal K should be accessed through that server instead of opened twice.

For a Signal K server requiring authentication, put a read token in the
`DSC_SIGNALK_TOKEN` environment variable; it is not printed in JSON. Use the
server's HTTPS URL where available. Redirects are rejected, including when
authentication is enabled; supply the final server root URL directly.

### Generate a position message

Replace `YOUR_MMSI`, `PEER_MMSI` and `YOUR_CALL` below with the identities
agreed for your experiment. For amateur experiments, see the address
convention below. Callsigns are not converted into MMSIs.

```sh
dsc encode-position position.wav --source YOUR_MMSI --destination PEER_MMSI --latitude 50.2 --longitude -4.5 --callsign YOUR_CALL --locator auto
dsc decode position.wav --centre 1700
```

`encode-position` produces the **positive safety position-request
acknowledgement** defined in M.493-16 Table A1-4.7: individual format 120,
category 108, telecommands 121/126, position, UTC and EOS 122. It is the reply
format, not a new unsolicited broadcast message type. The command constructs
the reply for an experiment; it neither waits for a request nor automatically
responds to received calls. It can also be used to compose a reply to a
separately received request. Receiver acceptance of standalone replies is
part of the experiment.

Position is rounded to the nearest whole minute of latitude/longitude;
UTC uses the fix's hour/minute. No date is carried on air. The JSON separates
the original `position_fix` from the encoded `position_report`. Decoding
WAV or live audio now returns `position_report` for these supported replies
(otherwise null), alongside the existing `test_call` field. No M.821 fine
position expansion, unknown-position reply, or refused-position reply is
implemented yet. Generating an ordinary safety test call still uses
`encode-test`; that message has no position field.

### Amateur experimental address convention

For this project's **amateur-band experiments**, participants with an assigned
seven-digit personal amateur DMR ID can use the nine-digit address formed by
prepending `89`:

```text
experimental DSC address = "89" + seven-digit personal DMR ID
```

For arithmetic illustration only, `1234567` becomes `891234567`; that example
is not an address assignment. Use your own assigned ID, and use the peer's
corresponding address as the destination. Distinct seven-digit DMR IDs produce
distinct addresses, without a hash or a second project registry. Use the
personal ID, not a repeater ID, talkgroup number or hotspot ID with a suffix;
do not pad or truncate a different-length ID to fit. See
[BrandMeister's explanation of personal and hotspot IDs](https://news.brandmeister.network/using-dmr-ids-on-brandmeister/).

The extra `9` is significant. Under
[ITU-R M.585-10 (April 2026), Annex 2 Section 1](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.585-10-202604-I!!PDF-E.pdf),
handheld VHF DSC identities have the form `8MIDxxxxx`, with digits 2-4
containing an allocated country MID. The
[ITU MID allocation table](https://www.itu.int/gladapp/Allocation/MIDs)
checked on **21 November 2026** lists country MIDs starting with 2 through 7,
and none starting with 9. Thus an `89.......` address does not match the
currently allocated handheld format, nor the other assigned maritime identity
formats. This is the numbering rationale; it does not depend on the absence
of handheld MF/HF radios.

This is a **project convention for an experimental DSC address**, not an
ITU allocation or a genuine MMSI. It makes no guarantee against future
allocations or someone else using an invented number. Keep it on the agreed
amateur frequencies, retain the CW amateur callsign, and do not substitute it
for a vessel's assigned maritime identity. Receiver acceptance of an address
with an unallocated MID is itself something to test.

At present, supply the resulting nine digits to the existing `--source` and
`--destination` options. Those option names and JSON fields remain compatible
with ordinary DSC; the software does not look up or verify DMR registration,
authenticate the caller, or enforce the amateur-only convention. Prefix `89`
is not globally reserved for this project, so a decoder must not assume that
every received number with that prefix is necessarily a participating ham.

### Play once to an output device

Add `--play --device 3` to either encoder, selecting an **output** device
from `dsc devices`. A unique name substring such as `--device "USB Audio"`
also works. With `--play` but no `--device`, the system default output is
used. The WAV path is optional when playing, and supplying both saves and
plays the same generated waveform. For example:

```sh
dsc encode-test test.wav --source YOUR_MMSI --destination PEER_MMSI --callsign YOUR_CALL --play --device 3
dsc encode-position --source YOUR_MMSI --destination PEER_MMSI --position-source serial --serial-port COM3 --callsign YOUR_CALL --locator auto --play --device 3
```

`--output-channel 1` selects left/mono; `2` selects right, leaving other
opened channels silent. Playback resamples to the device's default sample
rate; `--sample-rate` still controls the WAV synthesis rate. **Ctrl+C** aborts
playback (exit 130), and an output underflow stops the burst without repeating
it. Playback status goes to stderr; successful completion emits JSON.

This is soundcard output only: it does **not** set frequency, operate PTT or
enforce amateur-band limits. Radio configuration and keying remain manual.
The optional CW prefix identifies the amateur station but is not a licence
check. These experiments require appropriate amateur operating privileges
and frequencies. Marine-radio reception and physical audio/GPS devices
still need testing; software tests use simulated ports and servers.

## Python functions

```python
from gmdss_dsc import TestCall, encode_frame, decode_frame
from gmdss_dsc.modem import modulate, write_wav, decode_wav

call = TestCall(source="215322000", destination="004634060")
frame = call.to_frame()
bits = encode_frame(frame, dot_bits=call.dot_bits)
assert decode_frame(bits).frame == frame

audio = modulate(bits, sample_rate=8000, centre_hz=975)
write_wav("example.wav", audio, 8000)
received = decode_wav("example.wav")[0]
assert TestCall.from_frame(received.decoded.frame) == call
```

- `symbols.py`: ten-bit characters, error detection, decimal MMSI packing.
- `framing.py`: phasing, DX/RX repetition, format repetition, EOS and ECC.
- `calls.py`: individual safety test calls and position acknowledgements.
- `modem.py`: continuous-phase FSK synthesis and offline tone-correlation decoding.
- `live.py`: optional soundcard capture, overlapping decode windows and deduplication.
- `position.py`: position providers, freshness checks and Maidenhead conversion.
- `transmit.py`: CW identification and optional soundcard playback.
- `cli.py`: WAV/live input and JSON results.

`Frame.information` contains the format symbol **once**, followed by the
information symbols. The framing encoder adds its physical repetition;
EOS and ECC are separate. MMSIs are strings so leading zeros survive.

## Protocol details worth making explicit

These conventions are from M.493-16, Annex 1, sections 1-4 and 9-10:

1. MF/HF uses 100 bits/s and a 170 Hz frequency shift. In this code,
   `1` means Y (lower tone), and `0` means B (upper tone).
2. A ten-bit character contains seven information bits, **least significant
   first**, then the three-bit count of zero information bits, **most
   significant first**. Mixing those two orders is an easy mistake.
3. Each information character has a delayed repeat. The two copies start
   five character slots apart: four intervening characters occupy 400 ms,
   while the start-to-start separation is 500 ms at 100 bits/s.
4. Initial phasing alternates DX 125 with RX 111 down to 106. RX 105 and
   104 accompany the first two DX format characters.
5. The format is sent twice in DX, but contributes **once** to the XOR
   error-check character. EOS also contributes once. ECC is a seven-bit
   XOR check, not a CRC or a proof that a message is correct.
6. The closing sequence sends EOS three times in DX and once in RX;
   ECC appears in both DX and RX.
7. Dotting is 20 or 200 bits according to the call. The supported coast
   test call and individual acknowledgement use 20; an initial test call
   addressed to a ship uses 200. `TestCall.dot_bits` selects this.

## What the baseline can and cannot do

The audio receiver resamples mono WAV input to 8 kHz, estimates one tone
pair, and correlates real audio against complex exponentials at the two
frequencies. Taking squared magnitudes gives a phase-insensitive decision.
A separate Hilbert transform is unnecessary for this implementation.
It tries all 80 sample positions within a bit period and both polarities.
`--centre` can override the coarse frequency estimate; automatic estimation
searches centres from 400 to below 3200 Hz and assumes one dominant channel.

The bit decoder can replace a character with its repeat when exactly one
copy fails the character check. It rejects two character-valid copies that
disagree, and requires a complete closing sequence and passing ECC. It
requires at least three consecutive exact phasing characters and valid
final RX phasing symbols. It does not implement the standard's more general
nonconsecutive phasing acquisition. Received timing is approximate;
`data_start_s` is the first format character, after dotting and phasing.

This is a fixed-clock, short-recording baseline: no clock-drift tracking,
multipath equalisation, soft error correction, uncertainty probabilities,
raw SDR I/Q input or VHF mode. Live soundcard reception wraps this same
fixed-clock decoder in overlapping windows. Individual safety test calls and
positive safety position acknowledgements have semantic encoders/parsers.
Other base-call symbol sequences may pass framing checks,
but their meaning is not interpreted; expansion messages are not handled.
The `tone_margin` output is a diagnostic score, **not a probability**.
This experimental software is not a marine safety receiver.

## Validation and next steps

Tests cover fixed wire vectors; all 128 symbols and all 1280 single-bit
corruptions; the published GMDSS_1 symbol sequence and ECC; repetition and
checksum failures; truncated messages; WAV round trips; timing phase;
polarity; 48 kHz input; and repeated calls. A two-bit corruption example
explicitly demonstrates the limitation of the character zero-count check.
CI runs the self-contained tests and decodes the checked-in synthesized WAV.
An optional integration test uses the original received GMDSS_1 sample.

`python examples/make_gmdss1.py` regenerates the example WAV and the
[inspectable symbol/bit trace](examples/gmdss1_trace.json). No RF equipment
is involved in any of these examples.

Next: check the synthetic audio through an independent receiver such as
SDRangel/YaDD, add more labelled real recordings and message types, then
retain per-bit likelihoods and evaluate fading/noise experiments. SDRangel
has more advanced decisions than this baseline; the current Python code
does not claim to reproduce its weak-signal performance.

## Sources

- [ITU-R M.493-16 (2023), official PDF](https://www.itu.int/dms_pubrec/itu-r/rec/m/R-REC-M.493-16-202312-I!!PDF-E.pdf).
- [SDRangel DSC demodulator](https://github.com/f4exb/sdrangel/blob/master/plugins/channelrx/demoddsc/dscdemodsink.cpp)
  and [DSC message parser](https://github.com/f4exb/sdrangel/blob/master/sdrbase/util/dsc.cpp).
- [PhosphorDSC tests](https://fractaldoctor.github.io/PhosphorDSC/tests.html),
  an independent published interpretation of the reference recording.
- [Signal Identification Wiki: GMDSS DSC](https://www.sigidwiki.com/wiki/GMDSS_Digital_Selective_Calling),
  source of the original received audio.

