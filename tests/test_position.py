import contextlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import unittest
from unittest.mock import patch

from gmdss_dsc.calls import PositionReport, encode_position, decode_position
from gmdss_dsc.framing import Frame, encode_frame, find_frames
from gmdss_dsc.position import (PositionError, PositionFix, UTC, maidenhead, nmea_fix,
                                serial_fix, gpsd_fix, signalk_fix, utc_time)


class PositionCodingTests(unittest.TestCase):
    def test_independent_symbol_vector(self):
        # M.493: quadrant 1, 50deg12'N, 004deg30'W = 1501200430.
        self.assertEqual(encode_position(50.2, -4.5), (15, 1, 20, 4, 30))
        call = PositionReport("215322000", "004634060", 50.2, -4.5, 12, 34)
        expected = (120, 0, 46, 34, 6, 0, 108, 21, 53, 22, 0, 0,
                    121, 126, 15, 1, 20, 4, 30, 126, 12, 34)
        frame = call.to_frame()
        self.assertEqual(frame.information, expected)
        self.assertEqual(frame.eos, 122)
        self.assertEqual(PositionReport.from_frame(Frame(expected, 122)), call)
        decoded = list(find_frames(encode_frame(frame, dot_bits=call.dot_bits)))
        self.assertEqual(decoded[0].frame, frame)

    def test_quadrants_rounding_and_limits(self):
        for lat, lon, first in ((50.2, 4.5, 5), (50.2, -4.5, 15),
                                 (-50.2, 4.5, 25), (-50.2, -4.5, 35)):
            symbols = encode_position(lat, lon)
            self.assertEqual(symbols[0], first)
            self.assertEqual(decode_position(symbols), (lat, lon))
        self.assertEqual(decode_position(encode_position(89.999, -179.999)), (90, -180))
        for lat, lon in ((91, 0), (0, 181), (float("nan"), 0), (True, 0)):
            with self.assertRaises(ValueError):
                encode_position(lat, lon)
        for symbols in ((99,)*5, (15, 6, 0, 4, 30), (19, 0, 10, 4, 30)):
            with self.assertRaises(ValueError):
                decode_position(symbols)
        with self.assertRaises(ValueError):
            PositionReport("215322000", "004634060", 0, 0, 24, 0)

    def test_known_locators_and_boundaries(self):
        for lat, lon, grid in ((0, 0, "JJ00AA"), (52, 0, "JO02AA"),
                               (51.5074, -0.1278, "IO91WM"),
                               (-90, -180, "AA00AA"), (90, 180, "RR99XX")):
            self.assertEqual(maidenhead(lat, lon), grid)


class FixTests(unittest.TestCase):
    def test_age_validation_and_timezone(self):
        now = datetime(2026, 11, 21, 12, tzinfo=UTC)
        stamp = utc_time("2026-11-21T13:00:00+01:00")
        self.assertEqual(stamp, now)
        fix = PositionFix(0, 0, stamp, "test")
        self.assertIs(fix.require_fresh(now=now), fix)
        for clock in (now+timedelta(seconds=31), now-timedelta(seconds=6)):
            with self.assertRaises(PositionError):
                fix.require_fresh(now=clock)
        with self.assertRaises(PositionError):
            utc_time("2026-11-21T12:00:00")

    def test_nmea_reference_sentences_and_invalid_fixes(self):
        rmc = b"$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        fix = nmea_fix(rmc)
        self.assertAlmostEqual(fix.latitude, 48.1173)
        self.assertAlmostEqual(fix.longitude, 11.5166666667)
        self.assertEqual(fix.time_utc, datetime(1994, 3, 23, 12, 35, 19, tzinfo=UTC))
        self.assertIsNone(nmea_fix(rmc[:-1]+b"B"))
        self.assertIsNone(nmea_fix(rmc.split(b"*")[0]))
        self.assertIsNone(nmea_fix(sentence("GPRMC,123519,V,4807.038,N,01131.000,E,0,0,230394,,,A")))
        self.assertIsNone(nmea_fix(sentence("GPRMC,123519,A,,N,,E,0,0,230394,,,A")))
        self.assertIsNone(nmea_fix(sentence("GPRMC,123519,A,4861,N,01131,E,0,0,230394,,,A")))
        self.assertIsNone(nmea_fix(sentence("GPGGA,123519,4807.038,N,01131.000,E,0,08,0.9,0,M,0,M,,")))

    def test_gga_midnight_uses_nearest_date(self):
        now = datetime(2026, 11, 21, 0, 0, 2, tzinfo=UTC)
        fix = nmea_fix(sentence("GPGGA,235959,4807.038,N,01131.000,E,1,08,0.9,0,M,0,M,,"), now=now)
        self.assertEqual(fix.time_utc, datetime(2026, 11, 20, 23, 59, 59, tzinfo=UTC))
        self.assertIn("date-inferred", fix.source)
        fix.require_fresh(now=now)

    def test_fragmented_serial_skips_stale_and_bad_data(self):
        now = datetime.now(UTC)
        body = f"GPRMC,{now:%H%M%S},A,5012.000,N,00430.000,W,0,0,{now:%d%m%y},,,A"
        good = sentence(body)+b"\r\n"
        bad = sentence(body.replace(f"{now:%d%m%y}", "010100"))+b"\r\n"
        chunks = [b"noise\n", bad, good[:7], good[7:20], good[20:]]
        class Port:
            in_waiting = 1
            closed = False
            def __enter__(self): return self
            def __exit__(self, *args): self.closed = True
            def read(self, size): return chunks.pop(0) if chunks else b""
        port = Port()
        with patch("serial.Serial", return_value=port):
            fix = serial_fix("COM3", timeout=0.5)
        self.assertEqual(fix.latitude, 50.2)
        self.assertEqual(fix.longitude, -4.5)
        self.assertTrue(port.closed)


def sentence(body):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}".encode("ascii")


@contextlib.contextmanager
def gpsd_server(messages):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    requests = []
    def run():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(2)
                requests.append(conn.recv(4096))
                payload = b"".join(json.dumps(m).encode()+b"\n" for m in messages)
                # Deliberately split lines across TCP writes.
                conn.sendall(payload[:11])
                conn.sendall(payload[11:])
        except OSError:
            pass
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1], requests
    finally:
        server.close()
        thread.join(timeout=3)


class NetworkPositionTests(unittest.TestCase):
    def test_gpsd_skips_partial_invalid_and_stale_tpv(self):
        now = datetime.now(UTC)
        good = {"class": "TPV", "mode": 3, "lat": 50.2, "lon": -4.5,
                "time": now.isoformat(), "device": "/dev/gps0"}
        messages = [{"class": "VERSION", "proto_major": 3},
                    {"class": "TPV", "mode": 3, "lat": 1},
                    dict(good, mode=1), dict(good, time="2000-01-01T00:00:00Z"),
                    dict(good, time=(now+timedelta(hours=1)).isoformat()), good]
        with gpsd_server(messages) as (port, requests):
            fix = gpsd_fix("127.0.0.1", port, timeout=2)
        self.assertEqual((fix.latitude, fix.longitude), (50.2, -4.5))
        self.assertIn(b"?WATCH=", requests[0])

    def test_gpsd_rejects_protocol_mismatch_and_eof(self):
        for messages in ([{"class": "VERSION", "proto_major": 9}], [{"class": "TPV", "mode": 1}]):
            with gpsd_server(messages) as (port, _):
                with self.assertRaises(PositionError):
                    gpsd_fix("127.0.0.1", port, timeout=2)

    def test_signalk_http_path_timestamp_auth_and_stale_cache(self):
        payload = {"value": {"latitude": 50.2, "longitude": -4.5},
                   "timestamp": datetime.now(UTC).isoformat(), "$source": "gps"}
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"DSC_SIGNALK_TOKEN": "test-only-token"}):
                url = f"http://127.0.0.1:{server.server_port}"
                fix = signalk_fix(url)
                self.assertEqual(fix.latitude, 50.2)
                self.assertEqual(requests[0], ("/signalk/v1/api/vessels/self/navigation/position", "Bearer test-only-token"))
                payload["timestamp"] = "2000-01-01T00:00:00Z"
                with self.assertRaisesRegex(PositionError, "stale"):
                    signalk_fix(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
