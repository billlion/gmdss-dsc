"""One-shot amateur experiments: allocation guard, CW + DSC and bounded CAT PTT.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import asdict, dataclass, field
import math
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace

from .calls import TestCall, PositionReport
from .framing import encode_frame
from .modem import modulate
from .position import get_fix, maidenhead
from .rig import RigError, usb_dial
from .transmit import identification, with_identification, PreparedPlayback


# Ofcom OFW611, Table C (Full), checked 2026-11-23; links and scope in docs/tx.md.
# Allocation edges, not preferred mode sub-bands. 60 m consists of separate blocks.
UK_BANDS_HZ = tuple((round(a * 1000), round(b * 1000)) for a, b in (
    (1810, 2000), (3500, 3800),
    (5258.5, 5264), (5276, 5284), (5288.5, 5292), (5298, 5307),
    (5313, 5323), (5333, 5338), (5354, 5358), (5362, 5374.5),
    (5378, 5382), (5395, 5401.5), (5403.5, 5406.5),
    (7000, 7200), (10100, 10150), (14000, 14350), (18068, 18168),
    (21000, 21450), (24890, 24990), (28000, 29700), (50000, 52000),
))
MAX_TX_SECONDS = 90
TX_WINDOW_HZ = 3000


class TxCancelled(RigError):
    pass


@dataclass(frozen=True)
class TxSettings:
    band_plan: str = ""  # explicit selection; an old RX profile never enables TX
    callsign: str = ""
    output_device: int | str | None = None
    output_channel: int = 1
    level: float = 0.25
    lead_s: float = 0.2
    tail_s: float = 0.2

    def validate(self, *, ready=False):
        if self.band_plan not in ("", "uk"):
            raise ValueError("Supported TX allocation plan: uk")
        if self.callsign:
            identification(self.callsign)
        if type(self.output_channel) is not int or not 1 <= self.output_channel <= 32:
            raise ValueError("Output channel must be an integer from 1 to 32")
        if self.output_device is not None and (type(self.output_device) not in (int, str)
                or isinstance(self.output_device, int) and self.output_device < 0):
            raise ValueError("Invalid output device")
        for name, value, low, high in (("level", self.level, 0.001, 1),
                                      ("lead", self.lead_s, 0, 2), ("tail", self.tail_s, 0, 2)):
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"TX {name} must be between {low} and {high}")
        if ready:
            if not self.band_plan or not self.callsign:
                raise ValueError("TX needs an explicit band plan and amateur callsign")
            if self.output_device is None or self.output_device == "":
                raise ValueError("TX needs an explicit radio output device; use 'dsc devices'")


def check_allocation(frequency_hz, centre_hz, band_plan, *, dial_hz=None):
    expected = usb_dial(frequency_hz, centre_hz)
    dial = expected if dial_hz is None else dial_hz
    if band_plan != "uk":
        raise ValueError("Select the uk TX allocation plan explicitly")
    if not math.isfinite(centre_hz) or not 700 <= centre_hz <= 2300:
        raise ValueError("TX audio centre must be 700–2300 Hz (1700 Hz recommended)")
    if not math.isfinite(dial) or abs((dial + centre_hz) - frequency_hz) > 2:
        raise RigError("TX frequency readback differs from the requested frequency")
    # Conservative 3 kHz USB window includes the carrier, CW and both FSK tones.
    # This is an engineering guard, not a measurement of the radio's emissions.
    if not any(low <= dial and dial + TX_WINDOW_HZ <= high for low, high in UK_BANDS_HZ):
        raise ValueError(f"TX blocked: USB window {dial/1000:.3f}–"
                         f"{(dial+TX_WINDOW_HZ)/1000:.3f} kHz is outside a UK amateur allocation")
    return dict(frequency_hz=frequency_hz, dial_hz=dial, mode="USB",
                centre_hz=centre_hz, checked_window_hz=[dial, dial + TX_WINDOW_HZ], band_plan=band_plan)


POSITION_DEFAULTS = dict(position_source="manual", latitude=None, longitude=None, position_time=None,
    serial_port=None, baud=4800, gpsd_host="localhost", gpsd_port=2947,
    signalk_url="http://localhost:3000", position_timeout=10.0, max_fix_age=30.0)


@dataclass(frozen=True)
class TxRequest:
    kind: str
    source: str
    destination: str
    frequency_hz: float
    settings: TxSettings
    centre_hz: float = 1700.0
    locator: str = ""
    cw_wpm: float = 18.0
    acknowledgement: bool = False
    position: dict = field(default_factory=dict)

    def prepare(self, *, fix_reader=get_fix):
        self.settings.validate(ready=True)
        plan = check_allocation(self.frequency_hz, self.centre_hz, self.settings.band_plan)
        if self.kind not in ("test", "position"):
            raise ValueError("Only test calls and position acknowledgements can be transmitted")
        # Validate addresses and CW before attempting any position I/O.
        TestCall(self.source, self.destination).to_frame()
        auto = self.locator.lower() == "auto"
        cw = identification(self.settings.callsign, None if auto else self.locator or None)
        if auto and self.kind != "position":
            raise ValueError("Automatic locator needs a position message")
        position = POSITION_DEFAULTS | self.position
        fix = None
        if self.kind == "position":
            fix = fix_reader(SimpleNamespace(**position))
            call = PositionReport(self.source, self.destination, fix.latitude, fix.longitude,
                                  fix.time_utc.hour, fix.time_utc.minute)
            if auto:
                cw = identification(self.settings.callsign, maidenhead(fix.latitude, fix.longitude))
        else:
            call = TestCall(self.source, self.destination, self.acknowledgement)
        frame = call.to_frame()
        rate = 8000
        audio = modulate(encode_frame(frame, dot_bits=call.dot_bits), sample_rate=rate,
                         centre_hz=self.centre_hz)
        audio = with_identification(audio, cw, sample_rate=rate, centre_hz=self.centre_hz,
                                    wpm=self.cw_wpm) * self.settings.level
        duration = len(audio) / rate
        if duration + self.settings.lead_s + self.settings.tail_s + 5 > MAX_TX_SECONDS:
            raise ValueError("CW + DSC transmission is too long (90 second limit including timeout margin)")
        encoded = PositionReport.from_frame(frame) if fix is not None else call
        record = dict(kind=self.kind, message=asdict(encoded), cw_text=cw, duration_s=duration,
                      output_device=self.settings.output_device, output_channel=self.settings.output_channel,
                      level=self.settings.level, **plan)
        if fix is not None:
            fix.require_fresh(position["max_fix_age"])
            record["position_fix"] = fix.as_dict()
        return PreparedMessage(self, audio, rate, record, fix, position["max_fix_age"])


@dataclass
class PreparedMessage:
    request: TxRequest
    audio: object
    sample_rate: int
    record: dict
    fix: object
    max_fix_age: float


class PTTGuard:
    """A second worker releases PTT on cancellation or the audio deadline.

    Serializes key-on and key-off so cancellation cannot be followed by a late
    key-on. Recovery uses a fresh TCP connection. No automatic retransmission.
    """
    def __init__(self, rig, cancel, timeout):
        self.rig, self.cancel, self.timeout = rig, cancel, timeout
        self._lock = Lock()
        self._done = Event()
        self._thread = None
        self.error = None

    def __enter__(self):
        try:
            with self._lock:
                if self.cancel.is_set():
                    raise TxCancelled("Transmission cancelled before keying")
                self.rig.key_ptt()
            self._thread = Thread(target=self._watch, name="dsc-ptt-watchdog", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self.release()
            raise

    def _watch(self):
        deadline = time.monotonic() + self.timeout
        while not self._done.wait(0.05):
            if self.cancel.is_set() or time.monotonic() >= deadline:
                self.cancel.set()
                try:
                    self.release()
                except RigError as exc:
                    self.error = exc
                return

    def release(self):
        with self._lock:
            self.rig.release_ptt()

    def __exit__(self, *args):
        self._done.set()
        try:
            self.release()
        finally:
            if self._thread is not None:
                self._thread.join()
        if self.error is not None and self.rig.ptt_uncertain:
            raise self.error


def transmit_once(rig, message, *, cancel=None, progress=lambda text: None,
                  playback_factory=PreparedPlayback):
    """Caller owns the station lock; capture must be stopped before entry.

    Leaves the radio at the selected amateur frequency, in VFO A / USB. Never
    retunes after a failed release. Receiver service resumes only on success.
    """
    cancel = cancel if cancel is not None else Event()
    request, settings = message.request, message.request.settings
    settings.validate(ready=True)
    check_allocation(request.frequency_hz, request.centre_hz, settings.band_plan)
    rig.require_tx()
    rig.check_idle()
    progress("Opening radio audio output")
    # Opening/resampling the device is completed before key-on. Playback does
    # not start until PTT has been confirmed. PTT is off before device close.
    with playback_factory(message.audio, message.sample_rate, device=settings.output_device,
                          channel=settings.output_channel) as playback:
        if cancel.is_set():
            raise TxCancelled("Transmission cancelled")
        progress("Checking radio and tuning USB")
        state = rig.prepare_tx(request.frequency_hz, request.centre_hz)
        check_allocation(request.frequency_hz, request.centre_hz, settings.band_plan,
                         dial_hz=state["dial_hz"])
        if message.fix is not None:
            message.fix.require_fresh(message.max_fix_age)
        timeout = message.record["duration_s"] + settings.lead_s + settings.tail_s + 5
        with PTTGuard(rig, cancel, timeout):
            progress("Transmitting CW identification and DSC")
            if cancel.wait(settings.lead_s):
                raise TxCancelled("Transmission cancelled")
            playback.play(cancel)
            if cancel.wait(settings.tail_s):
                raise TxCancelled("Transmission cancelled or timed out")
        if cancel.is_set():
            raise TxCancelled("Transmission cancelled or timed out")
    progress("Complete; PTT off confirmed")
    return message.record | dict(transmitted=True, ptt_off_confirmed=True, rig=state)
