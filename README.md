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
python -m pip install -e .
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
- `calls.py`: individual safety test calls and their acknowledgements.
- `modem.py`: continuous-phase FSK synthesis and offline tone-correlation decoding.
- `live.py`: optional soundcard capture, overlapping decode windows and deduplication.
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
fixed-clock decoder in overlapping windows. Only individual safety test calls have a semantic
encoder/parser. Other base-call symbol sequences may pass framing checks,
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
