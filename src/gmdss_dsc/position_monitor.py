"""Process-owned position polling; browser lifetimes do not own the GPS.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from copy import deepcopy
from threading import Condition, Lock, Thread
from time import monotonic
from types import SimpleNamespace

from .position import POSITION_DEFAULTS, PositionError, get_fix

UPDATE_DEFAULTS = dict(position_auto_update=False, position_interval=10.0)


def position_settings(values):
    """Validate scheduling without opening a device or requiring an initial fix."""
    settings = POSITION_DEFAULTS | UPDATE_DEFAULTS | deepcopy(values)
    if settings.keys() - (POSITION_DEFAULTS.keys() | UPDATE_DEFAULTS.keys()):
        raise ValueError("Unknown position setting")
    if settings["position_source"] not in ("manual", "serial", "gpsd", "signalk"):
        raise ValueError("Unknown position source")
    for key, maximum in (("position_timeout", 30), ("max_fix_age", 86400),
                         ("position_interval", 3600)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
            raise ValueError(f"{key} must be greater than zero and at most {maximum} seconds")
    if settings["position_interval"] < 1:
        raise ValueError("Update interval must be at least 1 second")
    if type(settings["position_auto_update"]) is not bool:
        raise ValueError("Automatic position updates must be true or false")
    if settings["position_auto_update"] and settings["position_source"] == "manual":
        raise ValueError("Automatic updates need serial NMEA, gpsd or Signal K, not manual coordinates")
    return settings


def _reader_settings(settings):
    return {key: settings[key] for key in POSITION_DEFAULTS}


class PositionMonitor:
    """One polling worker, a short state lock and serialized provider access.

    Never hold the state lock during I/O. TX can reuse a matching fresh fix or
    read under the same I/O lock, so two callers cannot open a serial GPS at once.
    All deadlines use monotonic time; fix freshness uses the original UTC stamp.
    """

    def __init__(self, reader=get_fix):
        self._reader = reader
        self._condition = Condition()
        self._io_lock = Lock()
        self._thread = None
        self._closed = False
        self._settings = None
        self._revision = 0
        self._requested = False
        self._next_read = 0.0
        self._pending = False
        self._fix = None
        self._error = ""

    def configure(self, values, *, read_now=False):
        settings = position_settings(values)
        with self._condition:
            if self._closed:
                raise ValueError("Server is shutting down")
            changed = settings != self._settings
            if read_now and (self._pending or self._requested) and not changed:
                raise ValueError("A position read is already in progress")
            if changed:
                if self._settings is None or _reader_settings(settings) != _reader_settings(self._settings):
                    self._fix, self._error = None, ""
                self._settings = settings
                self._revision += 1
                self._requested = False
                self._next_read = 0.0
            self._requested |= read_now
            if (read_now or settings["position_auto_update"]) and self._thread is None:
                self._thread = Thread(target=self._run, name="dsc-position", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def stop_updates(self):
        with self._condition:
            if self._settings is not None:
                self.configure(self._settings | {"position_auto_update": False})

    def _run(self):
        while True:
            with self._condition:
                while True:
                    if self._closed:
                        return
                    automatic = self._settings["position_auto_update"]
                    delay = self._next_read - monotonic()
                    if self._requested or (automatic and delay <= 0):
                        break
                    self._condition.wait(max(0, delay) if automatic else None)
                settings, revision = deepcopy(self._settings), self._revision
                self._requested = False
                self._pending = True
            try:
                with self._io_lock:
                    with self._condition:
                        if self._closed or revision != self._revision:
                            continue
                    fix = self._reader(SimpleNamespace(**_reader_settings(settings)))
                    fix.require_fresh(settings["max_fix_age"])
                    with self._condition:
                        if not self._closed and revision == self._revision:
                            self._fix, self._error = fix, ""
            except Exception as exc:
                with self._condition:
                    if not self._closed and revision == self._revision:
                        # A timeout/no-fix never erases the last valid timestamp.
                        self._error = str(exc)
            finally:
                with self._condition:
                    self._pending = False
                    if revision == self._revision:
                        self._next_read = monotonic() + settings["position_interval"]
                    self._condition.notify_all()

    def read_for_tx(self, args, cancel):
        """Use a matching fresh GPS fix, otherwise wait for exclusive provider I/O.

        Called from the existing TX worker, never from a browser callback.
        Cancelling while waiting does not key PTT or open a second GPS handle.
        """
        settings = position_settings({k: v for k, v in vars(args).items() if k in POSITION_DEFAULTS})
        key = _reader_settings(settings)

        def check_and_cached():
            with self._condition:
                if self._closed or cancel.is_set():
                    raise PositionError("Position read cancelled")
                if (settings["position_source"] != "manual" and self._fix is not None
                        and self._settings is not None and key == _reader_settings(self._settings)):
                    try:
                        return self._fix.require_fresh(settings["max_fix_age"])
                    except PositionError:
                        pass
            return None

        cached = check_and_cached()
        if cached is not None:
            return cached
        deadline = monotonic() + settings["position_timeout"]
        while not self._io_lock.acquire(timeout=0.1):
            cached = check_and_cached()
            if cached is not None:
                return cached
            if monotonic() >= deadline:
                raise PositionError("GPS is busy; position read timed out")
        try:
            cached = check_and_cached()
            if cached is not None:
                return cached
            fix = self._reader(SimpleNamespace(**key))
            fix.require_fresh(settings["max_fix_age"])
            with self._condition:
                if self._closed or cancel.is_set():
                    raise PositionError("Position read cancelled")
                if self._settings is not None and key == _reader_settings(self._settings):
                    self._fix, self._error = fix, ""
            return fix
        finally:
            self._io_lock.release()

    def snapshot(self):
        with self._condition:
            fix = self._fix.as_dict() if self._fix is not None else None
            if fix is not None:
                fix["fresh"] = -5 <= fix["age_s"] <= self._settings["max_fix_age"]
            return deepcopy(dict(position=fix, position_pending=self._pending or self._requested,
                                 position_error=self._error, position_settings=self._settings,
                                 position_revision=self._revision))

    def close(self, timeout=5.0):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
        # Provider I/O has bounded timeouts. A stuck DNS/driver call must not
        # prevent process exit; a late result is discarded after close().
