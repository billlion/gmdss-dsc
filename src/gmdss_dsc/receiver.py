"""Process-owned receiver state, independent of browser connections.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from threading import Event, Lock, Thread
from types import SimpleNamespace

from .live import WindowDecoder, listen
from .position import get_fix
from .records import result_dict


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

    def __init__(self, *, listener=listen, fix_reader=get_fix, history_size=200):
        self._listen = listener
        self._get_fix = fix_reader
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._position_thread = None
        self._closed = False
        self._state = "Stopped"
        self._error = ""
        self._settings = ReceiverSettings()
        self._session = 0
        self._count = 0
        self._messages = deque(maxlen=history_size)
        self._events = deque(maxlen=100)
        self._fix = None
        self._fix_error = ""
        self._fix_limit = 30.0
        self._fix_pending = False

    def log(self, message):
        with self._lock:
            self._events.append(f"{datetime.now(timezone.utc):%H:%M:%S} UTC  {message}")

    def start(self, settings):
        settings.validate()
        with self._lock:
            if self._closed:
                raise ValueError("Server is shutting down")
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("The shared receiver is already running or stopping")
            self._settings = settings
            self._session += 1
            self._stop.clear()
            self._state, self._error = "Starting", ""
            self._thread = Thread(target=self._receive, name="dsc-receiver", daemon=True)
            self._thread.start()

    def _started(self):
        with self._lock:
            if not self._stop.is_set():
                self._state = "Receiving"

    def _emit(self, result, segment, decoded_at):
        record = result_dict(result)
        with self._lock:
            self._count += 1
            record.update(id=self._count, receiver_session=self._session,
                          capture_segment=segment, decoded_at_utc=decoded_at)
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
            ))

    def jsonl(self):
        return "".join(json.dumps(r, allow_nan=False) + "\n"
                       for r in self.snapshot()["messages"])

    def close(self, timeout=5.0):
        with self._lock:
            self._closed = True
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        # Position I/O has its own deadline and never owns the audio device.
        # It is a daemon so a failed DNS/driver call cannot prevent process exit.
