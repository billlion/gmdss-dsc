from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import numpy as np

from gmdss_dsc.live import AudioBlock, AudioInputError, capture_blocks, listen
from gmdss_dsc.modem import demodulate, read_wav
from gmdss_dsc.position import PositionFix
from gmdss_dsc.receiver import ReceiverService, ReceiverSettings


class ReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs, cls.audio = read_wav(Path(__file__).resolve().parents[1] / "examples/gmdss1_clean.wav")
        cls.result = demodulate(cls.audio, cls.fs, centre_hz=975)[0]

    def test_one_worker_shared_history_stop_restart_and_bounded_export(self):
        entered = Event()
        exited = Event()

        def listener(*, emit, on_started, stop_event, **kwargs):
            on_started()
            for _ in range(3):
                emit(self.result, 0, datetime.now(timezone.utc).isoformat())
            entered.set()
            stop_event.wait(3)
            exited.set()

        service = ReceiverService(listener=listener, history_size=2)
        self.addCleanup(service.close)
        service.start(ReceiverSettings())
        self.assertTrue(entered.wait(2))
        self.assertEqual(service.snapshot()["state"], "Receiving")
        with self.assertRaisesRegex(ValueError, "already"):
            service.start(ReceiverSettings())
        first = service.snapshot()
        second = service.snapshot()
        self.assertEqual(first, second)
        first["messages"][0]["ecc"] = -1
        self.assertEqual(service.snapshot()["messages"][0]["ecc"], 85)
        records = [json.loads(s) for s in service.jsonl().splitlines()]
        self.assertEqual([r["id"] for r in records], [2, 3])
        self.assertEqual(records[0]["receiver_session"], 1)
        service.stop()
        self.assertTrue(exited.wait(2))
        service._thread.join(2)
        self.assertEqual(service.snapshot()["state"], "Stopped")
        entered.clear()
        service.start(ReceiverSettings())
        self.assertTrue(entered.wait(2))
        self.assertEqual(service.snapshot()["messages"][-1]["receiver_session"], 2)
        service.close()
        self.assertFalse(service._thread.is_alive())
        with self.assertRaisesRegex(ValueError, "shutting"):
            service.start(ReceiverSettings())

    def test_worker_error_is_visible_and_recoverable(self):
        def failed(**kwargs):
            raise AudioInputError("Input unavailable")
        service = ReceiverService(listener=failed)
        self.addCleanup(service.close)
        service.start(ReceiverSettings())
        service._thread.join(2)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["state"], "Error")
        self.assertIn("Input unavailable", snapshot["error"])
        service.start(ReceiverSettings())  # failure does not lock out retries
        service._thread.join(2)

    def test_invalid_settings_do_not_launch_worker(self):
        service = ReceiverService()
        for config in (ReceiverSettings(channel=0), ReceiverSettings(sample_rate=8000.5),
                       ReceiverSettings(centre_hz=float("nan")), ReceiverSettings(channel=1.5)):
            with self.subTest(config=config), self.assertRaises(ValueError):
                service.start(config)
        self.assertIsNone(service._thread)

    def test_idle_capture_cancellation_closes_audio(self):
        opened, closed, stop = Event(), Event(), Event()
        class Stream:
            active = True
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                opened.set()
                return self
            def __exit__(self, *args):
                closed.set()
        sd = SimpleNamespace(InputStream=Stream, PortAudioError=RuntimeError,
                             check_input_settings=lambda **kwargs: None)
        with patch("gmdss_dsc.live.load_sounddevice", return_value=sd):
            thread = Thread(target=lambda: list(capture_blocks(stop_event=stop)), daemon=True)
            thread.start()
            self.assertTrue(opened.wait(2))
            stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(closed.is_set())

    def test_cancel_flushes_after_device_closed(self):
        stop, closed = Event(), Event()
        emitted = []
        def source(**kwargs):
            try:
                yield AudioBlock(0, self.audio)
                stop.set()
                yield AudioBlock(len(self.audio), np.zeros(1))
            finally:
                closed.set()
        def emit(*args):
            self.assertTrue(closed.is_set())
            emitted.append(args)
        with patch("gmdss_dsc.live.capture_blocks", source):
            listen(emit=emit, log=lambda s: None, sample_rate=self.fs,
                   centre_hz=975, interval_s=10, stop_event=stop)
        self.assertEqual(len(emitted), 1)

    def test_shared_position_ages_and_new_failure_clears_old_fix(self):
        now = datetime.now(timezone.utc)
        fix = PositionFix(50.3, -4.2, now, "manual")
        service = ReceiverService(fix_reader=lambda _: fix)
        self.addCleanup(service.close)
        settings = dict(position_timeout=1, max_fix_age=30)
        service.read_position(settings)
        service._position_thread.join(2)
        self.assertTrue(service.snapshot()["position"]["fresh"])
        with patch("gmdss_dsc.position.datetime") as clock:
            clock.now.return_value = now + timedelta(seconds=31)
            self.assertFalse(service.snapshot()["position"]["fresh"])
        def failure(_):
            raise ValueError("No valid fix")
        service._get_fix = failure
        service.read_position(settings)
        service._position_thread.join(2)
        snapshot = service.snapshot()
        self.assertIsNone(snapshot["position"])
        self.assertEqual(snapshot["position_error"], "No valid fix")

    def test_one_pending_position_read(self):
        entered, release = Event(), Event()
        def reader(_):
            entered.set()
            release.wait(2)
            return PositionFix(0, 0, datetime.now(timezone.utc), "manual")
        service = ReceiverService(fix_reader=reader)
        self.addCleanup(service.close)
        try:
            service.read_position(dict(position_timeout=1, max_fix_age=30))
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(ValueError, "already"):
                service.read_position(dict(position_timeout=1, max_fix_age=30))
        finally:
            release.set()
            service._position_thread.join(2)
