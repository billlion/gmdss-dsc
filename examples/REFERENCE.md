# Repeating the received-audio check

The first reference is `GMDSS_1.mp3` from Signal Identification Wiki:

https://www.sigidwiki.com/images/e/e1/GMDSS_1.mp3

The original file used here is 16010 bytes; SHA-256:

```
daad55fba3d76f88f52ac9edd568cd31cb873de0161f8d3e05dbf355cd3b9ae8
```

Download it into a local directory, verify the hash, and convert to mono
8 kHz PCM with FFmpeg. From the repository root, for example:

```sh
mkdir -p local_samples
curl -L --fail https://www.sigidwiki.com/images/e/e1/GMDSS_1.mp3 -o local_samples/GMDSS_1.mp3
python -c 'import hashlib; from pathlib import Path; assert hashlib.sha256(Path("local_samples/GMDSS_1.mp3").read_bytes()).hexdigest() == "daad55fba3d76f88f52ac9edd568cd31cb873de0161f8d3e05dbf355cd3b9ae8"'
ffmpeg -i local_samples/GMDSS_1.mp3 -ac 1 -ar 8000 -c:a pcm_s16le local_samples/GMDSS_1_8k.wav
dsc decode local_samples/GMDSS_1_8k.wav
DSC_REFERENCE_WAV=local_samples/GMDSS_1_8k.wav python -m unittest discover -s tests -v
```

Expected: exactly one call, source `215322000`, destination `004634060`,
EOS 117 and ECC 85. The complete deinterleaved reference, retaining both
format symbols but only one EOS and ECC, is:

```
120 120 000 046 034 006 000 108 021 053 022 000 000 118
126 126 126 126 126 126 126 117 085
```

These values match the independent
[PhosphorDSC published decode](https://fractaldoctor.github.io/PhosphorDSC/tests.html).
The frame API stores only one copy of format 120, and stores EOS separately.

Local validation on 2026-11-06 found a centre near 976.5 Hz, first format
character near 2.071 s, and the end near 7.071 s in the 7.992 s converted
recording. These timing/frequency estimates are diagnostics; they are not
fixed test expectations. Decoding required no replacement of a bad character
by its repeat. The receiver's RF tuning and propagation path are not known.

The synthetic `gmdss1_clean.wav` is 6.9 s long: 0.25 s silence, 20 dotting
bits, 62 ten-bit physical characters, and 0.25 s silence. It uses centre
975 Hz and continuous phase. Message symbols, rate and shift match the
reference; channel effects and recording margins are deliberately absent.

The original MP3/converted WAV are excluded from git. `gmdss1_clean.wav`
and `gmdss1_trace.json` are generated from this project's own source.
The synthetic WAV has not yet been tested by running SDRangel or YaDD.
