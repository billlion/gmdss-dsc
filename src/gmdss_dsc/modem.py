"""Offline, fixed-clock MF/HF FSK baseline. No weak-signal claims.

Real audio is correlated directly with complex tone templates. This already
supplies quadrature information; a separate Hilbert transform is unnecessary.
The returned tone margin is a diagnostic score, not a probability.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from math import gcd
from pathlib import Path
import numpy as np
from scipy import signal
from scipy.io import wavfile
from .symbols import validate_bits
from .framing import DecodedFrame, find_frames


def modulate(bits, *, sample_rate=8000, centre_hz=1700.0, amplitude=0.6,
             phase=0.0, leading_s=0.25, trailing_s=0.25):
    """Continuous-phase FSK: bit 1 is centre-85 Hz, bit 0 centre+85 Hz.

Returns float samples. CPFSK is our synthesis choice; the decoder does not
assume phase continuity in externally recorded transmissions.
"""
    b = np.asarray(validate_bits(bits), dtype=float)
    if not isinstance(sample_rate, int) or sample_rate <= 0 or sample_rate % 100:
        raise ValueError("Synthesis sample rate must be a positive multiple of 100 Hz")
    if not np.isfinite(centre_hz) or not 85 < centre_hz < sample_rate/2-85:
        raise ValueError("Both tones must lie strictly between DC and Nyquist")
    if not np.isfinite(amplitude) or not 0 < amplitude <= 1:
        raise ValueError("amplitude must be in (0, 1]")
    if not all(np.isfinite(v) and v >= 0 for v in (leading_s, trailing_s)) or not np.isfinite(phase):
        raise ValueError("Padding must be nonnegative and phase finite")
    frequency = np.repeat(centre_hz + 85*(1-2*b), sample_rate//100)
    angles = phase + 2*np.pi/sample_rate * np.r_[0.0, np.cumsum(frequency[:-1])]
    audio = amplitude*np.cos(angles) if len(b) else np.zeros(0)
    return np.r_[np.zeros(round(leading_s*sample_rate)), audio,
                 np.zeros(round(trailing_s*sample_rate))]


def write_wav(path, audio, sample_rate):
    if not isinstance(sample_rate, (int, np.integer)) or sample_rate <= 0:
        raise ValueError("Sample rate must be a positive integer")
    a = np.asarray(audio, dtype=float)
    if a.ndim != 1 or not np.all(np.isfinite(a)) or np.max(np.abs(a), initial=0) > 1:
        raise ValueError("Audio must be finite mono samples in [-1, 1]")
    wavfile.write(Path(path), sample_rate, np.rint(a*32767).astype(np.int16))


def read_wav(path):
    fs, samples = wavfile.read(Path(path))
    if samples.ndim != 1:
        raise ValueError("Use a mono WAV file")
    if samples.dtype == np.uint8:
        audio = (samples.astype(float)-128)/128
    elif np.issubdtype(samples.dtype, np.signedinteger):
        audio = samples.astype(float) / (2**(np.iinfo(samples.dtype).bits-1))
    elif np.issubdtype(samples.dtype, np.floating):
        audio = samples.astype(float)
    else:
        raise ValueError("Unsupported WAV sample encoding")
    return fs, audio


def estimate_centre(audio, sample_rate=8000):
    """Coarse strongest 170-Hz peak-pair estimate, not an AFC tracker."""
    if len(audio) < 800:
        raise ValueError("Need at least 0.1 seconds of 8 kHz audio")
    freq, power = signal.welch(audio, sample_rate, nperseg=min(len(audio),8192))
    centres = np.arange(400.0, 3200.0, 0.5)
    score = np.interp(centres-85, freq, power)*np.interp(centres+85, freq, power)
    return float(centres[np.argmax(score)])


@dataclass(frozen=True)
class AudioFrame:
    decoded: DecodedFrame
    data_start_s: float
    end_s: float
    centre_hz: float
    inverted: bool
    tone_margin: float


def demodulate(audio, sample_rate, *, centre_hz=None):
    """Return complete ECC-passing calls found in a short mono recording.

Uses 8 kHz internally, a fixed 100-bit/s clock, all 80 sample phases and
both tone polarities. No clock-drift tracking, equalisation or soft repair.
Auto-centre estimation assumes one dominant DSC channel in the recording.
"""
    a = np.asarray(audio, dtype=float)
    if a.ndim != 1 or not np.all(np.isfinite(a)):
        raise ValueError("Audio must be finite and mono")
    if not isinstance(sample_rate, (int, np.integer)) or sample_rate < 1000:
        raise ValueError("Invalid audio sample rate")
    if len(a) == 0:
        return []
    fs = 8000
    if sample_rate != fs:
        divisor = gcd(int(sample_rate),fs)
        a = signal.resample_poly(a,fs//divisor,int(sample_rate)//divisor)
    a = a - a.mean()
    if len(a) < 800 or np.max(np.abs(a)) < 1e-12:
        return []
    fc = estimate_centre(a) if centre_hz is None else float(centre_hz)
    if not np.isfinite(fc) or not 85 < fc < fs/2-85:
        raise ValueError("Both tones must lie in the internal 8 kHz audio passband")
    n = fs//100
    t = np.arange(len(a))/fs
    energy = []
    for f in (fc-85,fc+85):
        cumulative = np.r_[0j, np.cumsum(a*np.exp(-2j*np.pi*f*t))]
        energy.append(np.abs(cumulative[n:]-cumulative[:-n])**2)
    soft = (energy[0]-energy[1])/(energy[0]+energy[1]+1e-20)
    found = []
    for offset in range(n):
        samples = soft[offset::n]
        for inverted in (False,True):
            bits = (samples < 0 if inverted else samples > 0).astype(np.uint8)
            for decoded in find_frames(bits):
                start = (offset+decoded.data_start_bit*n)/fs
                end = (offset+decoded.end_bit*n)/fs
                margin = float(np.mean(np.abs(samples[decoded.data_start_bit:decoded.end_bit])))
                found.append(AudioFrame(decoded,start,end,fc,inverted,margin))
    # Multiple sample phases usually decode the same burst. Keep the largest
    # tone margin. Distinct repeated calls later in the file remain distinct.
    selected = []
    for item in sorted(found,key=lambda r:r.tone_margin,reverse=True):
        if not any(abs(item.data_start_s-r.data_start_s)<0.1 and
                   item.decoded.frame == r.decoded.frame for r in selected):
            selected.append(item)
    return sorted(selected,key=lambda r:r.data_start_s)


def decode_wav(path, *, centre_hz=None):
    fs, audio = read_wav(path)
    return demodulate(audio, fs, centre_hz=centre_hz)
