"""CW identification and explicit soundcard playback; no rig control or PTT.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from math import gcd
from threading import Event
import re
import numpy as np
from scipy.signal import resample_poly
from .live import load_sounddevice, AudioInputError


class PreparedPlayback:
    """Prepare an output stream without starting it; callback playback is bounded.

    The rig TX caller handles PTT before closing this stream, even if a driver
    stalls in abort/close. PortAudio's finished callback means the final output
    buffer has drained after CallbackStop.
    """
    def __init__(self, audio, sample_rate, *, device, channel):
        self.audio = np.asarray(audio, dtype=np.float32)
        if (self.audio.ndim != 1 or not len(self.audio) or not np.all(np.isfinite(self.audio))
                or np.max(np.abs(self.audio)) > 1):
            raise ValueError("Playback requires nonempty finite mono audio in [-1, 1]")
        self.sample_rate, self.device, self.channel = sample_rate, device, channel
        self.stream = None
        self.finished = Event()
        self.cancel = Event()
        self.error = None
        self.index = 0

    def __enter__(self):
        self.sd = load_sounddevice()
        try:
            info = self.sd.query_devices(self.device, "output")
            rate = round(info["default_samplerate"])
            if self.channel > info["max_output_channels"]:
                raise ValueError("Selected output device does not have that channel")
            self.sd.check_output_settings(device=self.device, channels=self.channel,
                                          samplerate=rate, dtype="float32")
            if rate != self.sample_rate:
                divisor = gcd(rate, self.sample_rate)
                self.audio = resample_poly(self.audio, rate//divisor,
                                          self.sample_rate//divisor).astype(np.float32)
            self.stream = self.sd.OutputStream(device=self.device, channels=self.channel,
                samplerate=rate, dtype="float32", callback=self._callback,
                finished_callback=self.finished.set)
            return self
        except self.sd.PortAudioError as exc:
            raise AudioInputError(f"Cannot open radio output: {exc}") from exc

    def _callback(self, outdata, frames, timing, status):
        outdata.fill(0)
        if self.cancel.is_set():
            raise self.sd.CallbackAbort
        if status:
            self.error = AudioInputError(f"Audio output failed: {status}; burst may be incomplete")
            raise self.sd.CallbackAbort
        count = min(frames, len(self.audio) - self.index)
        outdata[:count, self.channel-1] = self.audio[self.index:self.index+count]
        self.index += count
        if self.index == len(self.audio):
            raise self.sd.CallbackStop

    def play(self, cancel):
        self.cancel = cancel
        try:
            self.stream.start()
        except self.sd.PortAudioError as exc:
            raise AudioInputError(f"Cannot start radio output: {exc}") from exc
        while not self.finished.wait(0.05):
            if cancel.is_set():
                # Do not block in a driver abort before caller has released PTT.
                return
        if self.error is not None:
            raise self.error

    def __exit__(self, *args):
        if self.stream is not None:
            try:
                try:
                    self.stream.abort()
                finally:
                    self.stream.close()
            except self.sd.PortAudioError as exc:
                raise AudioInputError(f"Cannot close radio output: {exc}") from exc

MORSE = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/", (
    ".-", "-...", "-.-.", "-..", ".", "..-.", "--.", "....", "..", ".---",
    "-.-", ".-..", "--", "-.", "---", ".--.", "--.-", ".-.", "...", "-",
    "..-", "...-", ".--", "-..-", "-.--", "--..", "-----", ".----", "..---",
    "...--", "....-", ".....", "-....", "--...", "---..", "----.", "-..-.")))


def identification(callsign, locator=None):
    if callsign is None:
        if locator is not None:
            raise ValueError("--locator requires --callsign")
        return None
    callsign = callsign.strip().upper()
    if (not re.fullmatch(r"[A-Z0-9]+(?:/[A-Z0-9]+)*", callsign)
            or len(callsign) > 40 or not re.search("[A-Z]", callsign)
            or not re.search("[0-9]", callsign)):
        raise ValueError("Callsign must contain letters and digits, with optional / suffixes or prefixes")
    text = "DE "+callsign
    if locator is not None:
        locator = locator.upper()
        if not re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2})?", locator):
            raise ValueError("Locator must have four or six Maidenhead characters")
        text += " "+locator
    return text


def morse_audio(text, *, sample_rate=8000, tone_hz=1700.0, wpm=18.0):
    """PARIS timing, 1/3/7-unit spacing, with 5 ms raised-cosine edges."""
    if not np.isfinite(wpm) or not 5 <= wpm <= 60:
        raise ValueError("CW speed must be between 5 and 60 words per minute")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("Sample rate must be a positive integer")
    if not np.isfinite(tone_hz) or not 0 < tone_hz < sample_rate/2:
        raise ValueError("CW tone must lie between DC and Nyquist")
    words = text.upper().split()
    if not words or any(c not in MORSE for word in words for c in word):
        raise ValueError("CW text supports ASCII letters, digits, spaces and /")
    unit = sample_rate*1.2/wpm
    segments = []
    def silence(units):
        segments.append(np.zeros(round(units*unit)))
    for wi, word in enumerate(words):
        if wi:
            silence(7)
        for ci, char in enumerate(word):
            if ci:
                silence(3)
            for ei, element in enumerate(MORSE[char]):
                if ei:
                    silence(1)
                length = round(unit*(1 if element == "." else 3))
                tone = 0.6*np.sin(2*np.pi*tone_hz*np.arange(length)/sample_rate)
                edge = min(round(0.005*sample_rate), length//2)
                if edge > 1:
                    ramp = 0.5-0.5*np.cos(np.linspace(0, np.pi, edge))
                    tone[:edge] *= ramp
                    tone[-edge:] *= ramp[::-1]
                segments.append(tone)
    return np.concatenate(segments)


def with_identification(audio, text, *, sample_rate, centre_hz, wpm=18.0):
    if text is None:
        return audio
    cw = morse_audio(text, sample_rate=sample_rate, tone_hz=centre_hz, wpm=wpm)
    # The modem's existing leading padding and entire phasing sequence remain.
    return np.concatenate((np.zeros(round(0.25*sample_rate)), cw,
                           np.zeros(round(0.5*sample_rate)), audio))


def play_audio(audio, sample_rate, *, device=None, channel=1):
    """Play once on one selected output channel; resample to device default rate.

Blocking short writes make Ctrl+C responsive. An underflow aborts playback;
we never silently repeat a damaged burst. Other output channels stay silent.
"""
    audio = np.asarray(audio, dtype=np.float32)
    if (audio.ndim != 1 or not np.all(np.isfinite(audio))
            or np.max(np.abs(audio), initial=0) > 1):
        raise ValueError("Playback requires finite mono audio in [-1, 1]")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("Sample rate must be a positive integer")
    if not isinstance(channel, int) or channel < 1:
        raise ValueError("Output channel must be a positive one-based number")
    sd = load_sounddevice()
    try:
        info = sd.query_devices(device, "output")
        rate = round(info["default_samplerate"])
        if channel > info["max_output_channels"]:
            raise ValueError("Selected output device does not have that channel")
        sd.check_output_settings(device=device, channels=channel, samplerate=rate, dtype="float32")
        if rate != sample_rate:
            divisor = gcd(rate, sample_rate)
            audio = resample_poly(audio, rate//divisor, sample_rate//divisor).astype(np.float32)
        with sd.OutputStream(device=device, channels=channel, samplerate=rate, dtype="float32") as stream:
            try:
                step = max(1, rate//10)
                for start in range(0, len(audio), step):
                    samples = audio[start:start+step]
                    block = np.zeros((len(samples), channel), dtype=np.float32)
                    block[:, channel-1] = samples
                    if stream.write(block):
                        raise AudioInputError("Audio output underflow: playback stopped; burst may be incomplete")
            except BaseException:
                stream.abort()
                raise
    except sd.PortAudioError as exc:
        raise AudioInputError(f"Cannot play audio: {exc}. Check 'dsc devices' and --device/--output-channel") from exc
