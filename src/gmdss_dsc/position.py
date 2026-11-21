"""One-shot position fixes from manual input, NMEA, gpsd or Signal K.

Network adapters use the standard library. Serial dependencies are optional.
No source is opened until explicitly selected; fixes are never cached on disk.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

UTC = timezone.utc


class PositionError(ValueError):
    """No usable position is available from the selected source."""


def coordinates(latitude, longitude):
    if (isinstance(latitude, bool) or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
            or not math.isfinite(latitude) or not -90 <= latitude <= 90
            or not math.isfinite(longitude) or not -180 <= longitude <= 180):
        raise PositionError("Require finite latitude in [-90, 90] and longitude in [-180, 180]")


def utc_time(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("missing timezone")
        return stamp.astimezone(UTC)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PositionError("Position time must be ISO 8601 with a timezone, e.g. 2026-11-21T12:34:00Z") from exc


@dataclass(frozen=True)
class PositionFix:
    latitude: float
    longitude: float
    time_utc: datetime
    source: str

    def __post_init__(self):
        coordinates(self.latitude, self.longitude)
        if self.time_utc.tzinfo is None or self.time_utc.utcoffset() is None:
            raise PositionError("Position time must have a timezone")

    def require_fresh(self, max_age=30.0, now=None):
        if not math.isfinite(max_age) or max_age <= 0:
            raise PositionError("Maximum fix age must be positive and finite")
        age = ((now or datetime.now(UTC)) - self.time_utc).total_seconds()
        if age < -5:
            raise PositionError("Position timestamp is in the future; check the computer/GPS clocks")
        if age > max_age:
            raise PositionError(f"Position is stale ({age:.1f} seconds old; limit {max_age:g})")
        return self

    def as_dict(self):
        return {"latitude": self.latitude, "longitude": self.longitude,
                "time_utc": self.time_utc.astimezone(UTC).isoformat(), "source": self.source,
                "age_s": (datetime.now(UTC)-self.time_utc).total_seconds(),
                "locator": maidenhead(self.latitude, self.longitude)}


def maidenhead(latitude, longitude):
    """Six-character locator. Exact north/east limits use the last grid cell."""
    coordinates(latitude, longitude)
    # Integer cells avoid floating-point spill into an invalid letter at poles.
    x = min(8639, int(math.floor((longitude+180)*24)))
    y = min(4319, int(math.floor((latitude+90)*24)))
    xf, xr = divmod(x, 480)
    yf, yr = divmod(y, 240)
    xd, xs = divmod(xr, 48)
    yd, ys = divmod(yr, 24)
    return f"{chr(65+xf)}{chr(65+yf)}{xd}{yd}{chr(65+xs//2)}{chr(65+ys)}"


def _serial_modules():
    try:
        import serial
        import pynmea2
    except ImportError as exc:
        raise PositionError('Serial GPS needs: python -m pip install -e ".[gps]"') from exc
    return serial, pynmea2


def serial_ports():
    _serial_modules()
    from serial.tools.list_ports import comports
    return [{"device": p.device, "description": p.description} for p in comports()]


def nmea_fix(line, *, now=None):
    """Accept checksummed RMC or GGA with a valid fix and complete coordinates.

GGA has no date: choose the UTC date nearest the computer clock (also at
midnight). Prefer RMC at the receiver if absolute fix-date validation matters.
"""
    _, pynmea2 = _serial_modules()
    now = now or datetime.now(UTC)
    try:
        raw = line.decode("ascii").strip() if isinstance(line, (bytes, bytearray)) else line.strip()
        body, check = raw[1:].split("*")
        checksum = 0
        for char in body:
            checksum ^= ord(char)
        if not raw.startswith("$") or len(check) != 2 or int(check, 16) != checksum:
            return None
        msg = pynmea2.parse(raw)
        if msg.sentence_type not in ("RMC", "GGA"):
            return None
        if (not msg.lat or not msg.lon or msg.lat_dir not in ("N", "S")
                or msg.lon_dir not in ("E", "W")
                or float(msg.lat) < 0 or float(msg.lon) < 0
                or not 0 <= float(msg.lat) % 100 < 60
                or not 0 <= float(msg.lon) % 100 < 60):
            return None
        if msg.sentence_type == "RMC":
            if msg.status != "A" or getattr(msg, "mode_indicator", "") == "N":
                return None
            stamp = datetime.combine(msg.datestamp, msg.timestamp, UTC)
        else:
            if msg.gps_qual not in (1, 2, 3, 4, 5):
                return None
            today = datetime.combine(now.date(), msg.timestamp, UTC)
            stamp = min((today+timedelta(days=n) for n in (-1, 0, 1)),
                        key=lambda t: abs((t-now).total_seconds()))
        return PositionFix(msg.latitude, msg.longitude, stamp,
                           f"serial:{msg.sentence_type}" + (":date-inferred" if msg.sentence_type == "GGA" else ""))
    except (ValueError, TypeError, AttributeError, UnicodeError, pynmea2.ParseError):
        return None


def serial_fix(port, baud=4800, timeout=10.0, max_age=30.0):
    serial, _ = _serial_modules()
    if not port or baud <= 0:
        raise PositionError("Serial GPS requires --serial-port and a positive --baud")
    deadline = time.monotonic()+timeout
    last_error = "No valid RMC/GGA fix received"
    try:
        with serial.Serial(port, baudrate=baud, timeout=min(timeout, 0.2)) as stream:
            pending = bytearray()
            while time.monotonic() < deadline:
                # Preserve partial sentences across serial timeouts.
                pending.extend(stream.read(max(1, min(stream.in_waiting, 4096))))
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    fix = nmea_fix(line)
                    if fix is not None:
                        try:
                            return fix.require_fresh(max_age)
                        except PositionError as exc:
                            last_error = str(exc)
                if len(pending) > 4096:
                    pending.clear()
    except serial.SerialException as exc:
        raise PositionError(f"Cannot read serial GPS {port}: {exc}") from exc
    raise PositionError(f"Serial GPS timeout: {last_error}")


def gpsd_fix(host="localhost", port=2947, timeout=10.0, max_age=30.0):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise PositionError("gpsd port must be in 1..65535")
    deadline = time.monotonic()+timeout
    last_error = "No complete TPV fix received"
    try:
        with socket.create_connection((host, port), timeout=timeout) as stream:
            stream.sendall(b'?WATCH={"enable":true,"json":true};\n')
            pending = bytearray()
            while time.monotonic() < deadline:
                stream.settimeout(max(0.001, deadline-time.monotonic()))
                block = stream.recv(4096)
                if not block:
                    raise PositionError("gpsd disconnected before supplying a fix")
                pending.extend(block)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    try:
                        data = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("class") == "VERSION" and data.get("proto_major") != 3:
                        raise PositionError("This client requires gpsd JSON protocol version 3")
                    if data.get("class") != "TPV" or data.get("mode") not in (2, 3):
                        continue
                    try:
                        fix = PositionFix(data["lat"], data["lon"], utc_time(data["time"]),
                                          "gpsd:"+str(data.get("device", host)))
                        return fix.require_fresh(max_age)
                    except (KeyError, PositionError) as exc:
                        last_error = str(exc)
                if len(pending) > 65536:
                    raise PositionError("Oversized gpsd response")
    except OSError as exc:
        raise PositionError(f"gpsd connection failed or timed out: {exc}; {last_error}") from exc
    raise PositionError(f"gpsd timeout: {last_error}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward a bearer token to a redirect destination.


def signalk_fix(url="http://localhost:3000", timeout=10.0, max_age=30.0):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise PositionError("Signal K requires an http(s) server URL without credentials, query or fragment")
    endpoint = url.rstrip("/")+"/signalk/v1/api/vessels/self/navigation/position"
    headers = {"Accept": "application/json"}
    token = os.environ.get("DSC_SIGNALK_TOKEN")
    if token:
        headers["Authorization"] = "Bearer "+token
    try:
        with build_opener(_NoRedirect()).open(Request(endpoint, headers=headers), timeout=timeout) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise PositionError("Oversized Signal K response")
        data = json.loads(raw)
        value = data["value"]
        fix = PositionFix(value["latitude"], value["longitude"], utc_time(data["timestamp"]),
                          "signalk:"+str(data.get("$source", "self")))
        return fix.require_fresh(max_age)
    except HTTPError as exc:
        raise PositionError(f"Signal K HTTP {exc.code}; check the URL and DSC_SIGNALK_TOKEN if authentication is required") from exc
    except (URLError, OSError) as exc:
        raise PositionError("Cannot reach the Signal K server; check URL, connection and timeout") from exc
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PositionError):
            raise
        raise PositionError("Signal K did not return a timestamped navigation.position value") from exc


def get_fix(args):
    """Resolve a CLI position source to one validated, fresh snapshot."""
    if not math.isfinite(args.position_timeout) or args.position_timeout <= 0:
        raise PositionError("Position timeout must be positive and finite")
    if not math.isfinite(args.max_fix_age) or args.max_fix_age <= 0:
        raise PositionError("Maximum fix age must be positive and finite")
    source = args.position_source
    if source != "manual" and any(v is not None for v in
                                    (args.latitude, args.longitude, args.position_time)):
        raise PositionError("Manual coordinates/time cannot be combined with a live position source")
    if source == "manual":
        if args.latitude is None or args.longitude is None:
            raise PositionError("Manual position requires --latitude and --longitude")
        stamp = utc_time(args.position_time) if args.position_time else datetime.now(UTC)
        return PositionFix(args.latitude, args.longitude, stamp, "manual").require_fresh(args.max_fix_age)
    if source == "serial":
        return serial_fix(args.serial_port, args.baud, args.position_timeout, args.max_fix_age)
    if source == "gpsd":
        return gpsd_fix(args.gpsd_host, args.gpsd_port, args.position_timeout, args.max_fix_age)
    return signalk_fix(args.signalk_url, args.position_timeout, args.max_fix_age)
