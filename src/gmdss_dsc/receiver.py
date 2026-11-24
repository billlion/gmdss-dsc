"""Process-owned receiver state, independent of browser connections.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from threading import Event, Lock, Thread
from types import SimpleNamespace

from .live import WindowDecoder, listen
from .position import get_fix
from .records import result_dict
from .rig import RigController, usb_dial
from .ham_tx import transmit_once


@dataclass(frozen=True)
class ReceiverSettings:
    device: int | str | None = None
    sample_rate: int = 48000
    channel: int = 1
    centre_hz: float | None = 1700.0

    def validate(self):
        if not isinstance(self.channel, int) or not 1 <= self.channel <= 32:
            raise ValueError("Input channel must be an integer from 1 to 32")
        if not isinstance(self.sample_rate, int) or not 8000 <= self.sample_rate <= 192000:
            raise ValueError("Sample rate must be an integer from 8000 to 192000 Hz")
        WindowDecoder(self.sample_rate, centre_hz=self.centre_hz)


class ReceiverService:
    """One capture worker, one optional fix worker, and bounded in-memory history."""

    def __init__(self, *, listener=listen, fix_reader=get_fix, history_size=200, rig=None,
                 settings=None, tx_sender=transmit_once):
        self._listen = listener
        self._get_fix = fix_reader
        self._tx_sender = tx_sender
        self._lock = Lock()
        self._control_lock = Lock()
        self._rig = rig if rig is not None else RigController()
        self._rig_state = None
        self._rig_connected = False
        self._rig_busy = False
        self._rig_error = ""
        self._station = None
        self._stop = Event()
        self._thread = None
        self._position_thread = None
        self._closed = False
        self._state = "Stopped"
        self._error = ""
        self._settings = settings or ReceiverSettings()
        self._session = 0
        self._count = 0
        self._messages = deque(maxlen=history_size)
        self._events = deque(maxlen=100)
        self._fix = None
        self._fix_error = ""
        self._fix_limit = 30.0
        self._fix_pending = False
        self._tx_cancel = Event()
        self._tx_state = "Idle"
        self._tx_error = ""
        self._tx_result = None
        self._tx_active = False

    def log(self, message):
        with self._lock:
            self._events.append(f"{datetime.now(timezone.utc):%H:%M:%S} UTC  {message}")

    def start(self, settings):
        settings.validate()
        with self._operation():
            if self._rig.ptt_uncertain:
                raise ValueError("PTT state is uncertain: reception remains stopped")
            self._start(settings)

    def _start(self, settings):
        with self._lock:
            if self._closed:
                raise ValueError("Server is shutting down")
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("The shared receiver is already running or stopping")
            self._settings = settings
            if self._rig_state is not None:
                self._rig_state.pop("frequency_hz", None)
                self._rig_state.pop("centre_hz", None)
                if self._rig_state["mode"] == "USB" and settings.centre_hz is not None:
                    self._rig_state.update(centre_hz=settings.centre_hz,
                                           frequency_hz=self._rig_state["dial_hz"] + settings.centre_hz)
            self._session += 1
            self._stop.clear()
            self._state, self._error = "Starting", ""
            self._thread = Thread(target=self._receive, name="dsc-receiver", daemon=True)
            self._thread.start()

    @contextmanager
    def _operation(self):
        if not self._control_lock.acquire(blocking=False):
            raise ValueError("A shared receiver/radio operation is already in progress")
        try:
            with self._lock:
                if self._closed:
                    raise ValueError("Server is shutting down")
                self._rig_busy = True
            yield
        finally:
            with self._lock:
                self._rig_busy = False
            self._control_lock.release()

    def configure(self, settings, *, station=None):
        settings.validate()
        with self._operation(), self._lock:
            if self._rig_connected or (self._thread and self._thread.is_alive()):
                raise ValueError("Stop reception and disconnect the radio before loading a profile")
            self._settings = settings
            if station is not None:
                self._station = deepcopy(station)

    def radio(self, action, *, settings=None, frequency_hz=None, centre_hz=None):
        """Run on a worker thread, never NiceGUI's event loop.

        Stop/join capture before a change, flushing under the OLD metadata.
        Only restart after a successful readback and settling. A new input
        stream and decoder discard queued samples from the old frequency.
        """
        if action not in ("connect", "read", "tune", "disconnect"):
            raise ValueError("Unknown radio operation")
        if action == "tune":
            usb_dial(frequency_hz, centre_hz)
            replace(self._settings, centre_hz=centre_hz).validate()
        with self._operation():
            with self._lock:
                self._rig_error = ""
                running = self._thread is not None and self._thread.is_alive()
            try:
                if running:
                    self._stop_capture()
                    self._thread.join(timeout=15)
                    if self._thread.is_alive():
                        raise ValueError("Receiver is still stopping; radio settings were not changed")
                # Stopped capture has emitted its final old-frequency results.
                with self._lock:
                    self._rig_state = None
                if action == "connect":
                    state = self._rig.connect(settings)
                elif action == "read":
                    state = self._rig.read()
                elif action == "tune":
                    state = self._rig.tune(frequency_hz, centre_hz)
                    with self._lock:
                        self._settings = replace(self._settings, centre_hz=centre_hz)
                else:
                    self._rig.close()
                    state = None
                with self._lock:
                    self._rig_state = state
                self.log("Radio disconnected" if state is None else
                         f"Radio {action}: {state['dial_hz']/1000:.3f} kHz {state['mode']} confirmed")
                if running and action != "disconnect":
                    self._start(self._settings)
                return state
            except Exception as exc:
                with self._lock:
                    self._rig_error = str(exc)
                self.log(f"Radio error: {exc}; reception remains stopped")
                raise
            finally:
                with self._lock:
                    self._rig_connected = self._rig.connected

    def _started(self):
        with self._lock:
            if not self._stop.is_set():
                self._state = "Receiving"

    def transmit(self, request):
        """Run in a worker; one station operation across all browser clients."""
        with self._operation():
            with self._lock:
                if self._closed:
                    raise ValueError("Server is shutting down")
                if self._fix_pending:
                    raise ValueError("Wait for the current position read to finish")
                self._tx_cancel.clear()
                self._tx_state, self._tx_error, self._tx_result = "Preparing", "", None
                self._tx_active = True
                running = self._thread is not None and self._thread.is_alive()
            try:
                self._rig.require_tx()
                if running:
                    self._stop_capture()
                    self._thread.join(timeout=15)
                    if self._thread.is_alive():
                        raise ValueError("Receiver is still stopping; transmission cancelled")
                with self._lock:
                    self._rig_state = None
                message = request.prepare(fix_reader=self._get_fix)
                result = self._tx_sender(self._rig, message, cancel=self._tx_cancel,
                                        progress=self._tx_progress)
                with self._lock:
                    self._tx_result = result
                    self._rig_state = result["rig"]
                    self._settings = replace(self._settings, centre_hz=request.centre_hz)
                if running and not self._closed:
                    self._start(self._settings)
                return result
            except Exception as exc:
                with self._lock:
                    self._tx_state, self._tx_error = "Stopped", str(exc)
                    self._rig_error = str(exc) if self._rig.ptt_uncertain else ""
                self.log(f"TX stopped: {exc}")
                raise
            finally:
                with self._lock:
                    self._rig_connected = self._rig.connected
                    self._tx_active = False

    def _tx_progress(self, text):
        with self._lock:
            self._tx_state = text
        self.log(text)

    def abort_tx(self):
        self._tx_cancel.set()

    def _emit(self, result, segment, decoded_at):
        record = result_dict(result)
        with self._lock:
            self._count += 1
            record.update(id=self._count, receiver_session=self._session,
                          capture_segment=segment, decoded_at_utc=decoded_at)
            if self._rig_state is not None:
                record["rig"] = deepcopy(self._rig_state)
            self._messages.append(record)

    def _receive(self):
        try:
            self._listen(emit=self._emit, log=self.log, stop_event=self._stop,
                         on_started=self._started, **asdict(self._settings))
        except Exception as exc:
            with self._lock:
                self._state, self._error = "Error", str(exc)
            self.log(f"Receiver error: {exc}")
        else:
            with self._lock:
                self._state = "Stopped"

    def stop(self):
        with self._operation():
            self._stop_capture()

    def _stop_capture(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._state = "Stopping"
                self._stop.set()

    def read_position(self, settings):
        # A single shared operation: a second viewer cannot open the same GPS.
        settings = deepcopy(settings)
        timeout = float(settings["position_timeout"])
        max_age = float(settings["max_fix_age"])
        if not 0 < timeout <= 30 or not 0 < max_age <= 86400:
            raise ValueError("Use a position timeout of 0–30 s and a maximum age of 0–86400 s")
        with self._lock:
            if self._closed:
                raise ValueError("Server is shutting down")
            if self._rig_busy:
                raise ValueError("Wait for the current radio operation to finish")
            if self._position_thread is not None and self._position_thread.is_alive():
                raise ValueError("A position read is already in progress")
            self._fix, self._fix_error = None, ""
            self._fix_limit, self._fix_pending = max_age, True
            self._position_thread = Thread(target=self._read_position,
                                           args=(settings,), name="dsc-position", daemon=True)
            self._position_thread.start()

    def _read_position(self, settings):
        try:
            fix = self._get_fix(SimpleNamespace(**settings))
        except Exception as exc:
            with self._lock:
                self._fix_error = str(exc)
        else:
            with self._lock:
                self._fix = fix
        finally:
            with self._lock:
                self._fix_pending = False

    def _tx_block_reason(self):
        """Explain shared station state using cached data only; never do I/O."""
        if self._tx_active:
            return "A transmission is in progress. Use Abort TX to stop it."
        if self._rig.ptt_uncertain:
            return "PTT off is unconfirmed. Stop the transmitter locally and disconnect to retry release."
        if self._rig_busy:
            return "Wait for the current radio operation to finish."
        if self._fix_pending:
            return "Wait for the current position read to finish."
        if not self._rig_connected:
            return "Connect a local IC-7100 or dummy radio with CAT PTT enabled."
        settings = self._rig.settings
        if settings is not None and settings.connection == "external":
            return ("Use an existing rigctld supports reception only. Disconnect, stop the separate "
                    "rigctld process, then select Start a local rigctld and enable CAT PTT before reconnecting.")
        if settings is not None and settings.model not in (1, 3070):
            return "CAT TX currently supports the IC-7100 (model 3070) or Hamlib dummy (model 1)."
        if settings is None or not settings.allow_ptt:
            return "CAT PTT is disabled. Disconnect, tick Enable CAT PTT in Radio connection, and reconnect."
        return ""

    def snapshot(self):
        with self._lock:
            fix = self._fix.as_dict() if self._fix is not None else None
            if fix is not None:
                fix["fresh"] = -5 <= fix["age_s"] <= self._fix_limit
            return deepcopy(dict(
                state=self._state, error=self._error, settings=asdict(self._settings),
                count=self._count, messages=list(self._messages), events=list(self._events),
                position=fix, position_pending=self._fix_pending,
                position_error=self._fix_error,
                rig=self._rig_state, rig_connected=self._rig_connected,
                rig_busy=self._rig_busy, rig_error=self._rig_error,
                station=self._station,
                tx_state=self._tx_state, tx_error=self._tx_error, tx_result=self._tx_result,
                tx_active=self._tx_active,
                ptt_uncertain=self._rig.ptt_uncertain,
                tx_available=bool(self._rig.settings and self._rig.settings.allow_ptt),
                tx_block_reason=self._tx_block_reason(),
            ))

    def jsonl(self):
        return "".join(json.dumps(r, allow_nan=False) + "\n"
                       for r in self.snapshot()["messages"])

    def close(self, timeout=5.0):
        with self._lock:
            self._closed = True
        self.abort_tx()
        self._stop_capture()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        # A radio operation has bounded I/O deadlines; it sees _closed before
        # restarting capture. Wait for it, then close only our own helper.
        with self._control_lock:
            self._rig.close()
            with self._lock:
                self._rig_connected = False
                self._rig_state = None
        # Position I/O has its own deadline and never owns the audio device.
        # It is a daemon so a failed DNS/driver call cannot prevent process exit.
