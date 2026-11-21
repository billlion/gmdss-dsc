"""Soundcard capture and overlapping-window DSC decoding.

Capture stays in PortAudio's callback; decoding runs in the consuming thread.
This is a buffered wrapper around the offline modem, not a new demodulator.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
import queue

import numpy as np

from .modem import demodulate


class AudioInputError(RuntimeError):
    """An unavailable dependency/device or a stopped input stream."""


def load_sounddevice():
    # WAV-only users do not need sounddevice or a working PortAudio install.
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise AudioInputError(
            'Soundcard audio requires sounddevice and PortAudio. In the project '
            'directory run: python -m pip install -e ".[audio]". '
            'On Linux you may also need the libportaudio2 system package.'
        ) from exc
    except Exception as exc:
        # PortAudio also initializes during import. Its exception class is
        # unavailable when that import fails, e.g. with no running audio server.
        raise AudioInputError(
            f"Cannot initialize the audio backend: {exc}. "
            "Check that your operating system's audio service is running "
            "and this session has permission to access audio devices."
        ) from exc
    return sounddevice


def list_devices():
    sd = load_sounddevice()
    try:
        return str(sd.query_devices())
    except sd.PortAudioError as exc:
        raise AudioInputError(str(exc)) from exc


@dataclass(frozen=True)
class AudioBlock:
    # Index counts frames delivered to the callback, including queue drops.
    # Hardware overflows can lose an unknown number of frames: see overflow.
    first_sample: int
    samples: np.ndarray
    overflow: bool = False


def capture_blocks(*, device=None, sample_rate=48000, channel=1):
    """Yield mono blocks; a bounded queue keeps a slow decoder from growing RAM.

channel is one-based. Selecting channel 2 opens a two-channel stream and
extracts its right channel; channels are never deliberately averaged here.
"""
    sd = load_sounddevice()
    pending = queue.Queue(maxsize=100)  # roughly ten seconds at 100 ms/block
    sample_index = 0

    def callback(indata, frames, time_info, status):
        nonlocal sample_index
        first_sample = sample_index
        sample_index += frames
        block = AudioBlock(first_sample, indata[:, channel-1].copy(),
                           bool(status.input_overflow))
        try:
            pending.put_nowait(block)
        except queue.Full:
            # The next delivered index exposes this gap to the consumer.
            # Never print, decode, access files, or wait for the consumer here.
            pass

    try:
        sd.check_input_settings(device=device, channels=channel,
                                samplerate=sample_rate, dtype="float32")
        with sd.InputStream(device=device, channels=channel,
                            samplerate=sample_rate, dtype="float32",
                            blocksize=max(1, round(sample_rate/10)),
                            callback=callback) as stream:
            while True:
                try:
                    yield pending.get(timeout=0.25)
                except queue.Empty:
                    if not stream.active:
                        raise AudioInputError("Audio input stream stopped unexpectedly")
    except sd.PortAudioError as exc:
        raise AudioInputError(
            f"Cannot capture audio: {exc}. Check 'dsc devices', --device, "
            "--sample-rate and --channel."
        ) from exc


class WindowDecoder:
    """Decode every interval, retaining a bounded overlapping audio history.

Output times are seconds from the beginning of the current uninterrupted
capture segment. reset() starts a new segment after lost audio. Identical
messages at distinct times remain distinct receptions.
"""

    def __init__(self, sample_rate, *, centre_hz=1700.0,
                 window_s=30.0, interval_s=1.0):
        if not isinstance(sample_rate, int) or sample_rate < 1000:
            raise ValueError("Sample rate must be an integer of at least 1000 Hz")
        if not (math.isfinite(window_s) and math.isfinite(interval_s)
                and 0 < interval_s < window_s):
            raise ValueError("Require 0 < interval < window, in finite seconds")
        if centre_hz is not None and not (
                math.isfinite(centre_hz) and 85 < centre_hz < min(4000, sample_rate/2)-85):
            raise ValueError("Both tones must fit in the input and 8 kHz decoder passbands")
        self.sample_rate = sample_rate
        self.centre_hz = centre_hz
        self.window_samples = round(window_s * sample_rate)
        self.interval_samples = round(interval_s * sample_rate)
        if not 0 < self.interval_samples < self.window_samples:
            raise ValueError("Interval and window are too small for this sample rate")
        self.segment = -1
        self.reset()

    def reset(self):
        self.segment += 1
        self._parts = deque()
        self._buffered = 0
        self._received = 0
        self._next_decode = self.interval_samples
        self._last_decode = 0
        self._seen = []

    def feed(self, audio):
        """Accept any mono block size and return newly decoded AudioFrames."""
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim != 1 or not np.all(np.isfinite(audio)):
            raise ValueError("Live audio must be finite mono samples")
        results = []
        position = 0
        while position < len(audio):
            count = min(len(audio)-position, self._next_decode-self._received)
            self._parts.append(audio[position:position+count].copy())
            position += count
            self._received += count
            self._buffered += count
            while self._buffered > self.window_samples:
                excess = self._buffered-self.window_samples
                first = self._parts.popleft()
                removed = min(excess, len(first))
                self._buffered -= removed
                if removed < len(first):
                    self._parts.appendleft(first[removed:])
            if self._received == self._next_decode:
                results.extend(self._decode())
                self._next_decode += self.interval_samples
        return results

    def _decode(self):
        if not self._parts:
            return []
        offset = (self._received-self._buffered)/self.sample_rate
        self._seen = [(frame, start) for frame, start in self._seen
                      if start >= offset-0.1]
        results = []
        for result in demodulate(np.concatenate(self._parts), self.sample_rate,
                                 centre_hz=self.centre_hz):
            start = result.data_start_s+offset
            frame = result.decoded.frame
            if any(frame == old and abs(start-time) < 0.1
                   for old, time in self._seen):
                continue
            self._seen.append((frame, start))
            results.append(replace(result, data_start_s=start,
                                   end_s=result.end_s+offset))
        self._last_decode = self._received
        return results

    def flush(self):
        """Check an unfinished final interval when the user stops capture."""
        return self._decode() if self._received > self._last_decode else []


def listen(*, emit, log, device=None, sample_rate=48000, channel=1,
           centre_hz=1700.0, window_s=30.0, interval_s=1.0):
    """Capture until Ctrl+C. emit receives a result, segment and decode UTC."""
    if not isinstance(channel, int) or channel < 1:
        raise ValueError("Channel must be a positive one-based number")
    decoder = WindowDecoder(sample_rate, centre_hz=centre_hz,
                            window_s=window_s, interval_s=interval_s)
    blocks = capture_blocks(device=device, sample_rate=sample_rate, channel=channel)
    expected_sample = 0

    def output(results):
        for result in results:
            decoded_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            emit(result, decoder.segment, decoded_at)

    try:
        started = False
        for block in blocks:
            if not started:
                log(f"Listening at {sample_rate} Hz, channel {channel}; Ctrl+C to stop.")
                started = True
            if block.first_sample != expected_sample or block.overflow:
                output(decoder.flush())
                decoder.reset()
                log(f"Audio gap: discarding incomplete audio; starting capture segment "
                    f"{decoder.segment}. The decoder or input device could not keep up.")
            expected_sample = block.first_sample+len(block.samples)
            if block.overflow:
                # The location of hardware loss within this block is unknown.
                continue
            output(decoder.feed(block.samples))
    except KeyboardInterrupt:
        # Close the device before decoding the last partial interval.
        blocks.close()
        output(decoder.flush())
        log("Stopped.")
    finally:
        blocks.close()

