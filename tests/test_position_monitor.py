from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from gmdss_dsc.position import PositionError, PositionFix
from gmdss_dsc.position_monitor import PositionMonitor
from gmdss_dsc.receiver import ReceiverService, ReceiverSettings


SETTINGS = dict(position_source="serial", serial_port="COM7", position_timeout=1,
                max_fix_age=30, position_auto_update=True, position_interval=1)


def fix(latitude=50.3):
    return PositionFix(latitude, -4.2, datetime.now(timezone.utc), "serial:RMC")


class PositionMonitorTests(unittest.TestCase):
    def monitor(self, reader):
        monitor = PositionMonitor(reader)
        self.addCleanup(monitor.close)
        return monitor

    def settled(self, monitor):
        with monitor._condition:
            self.assertTrue(monitor._condition.wait_for(
                lambda: not monitor.snapshot()["position_pending"], timeout=2))

    def test_no_fix_retries_same_port_then_retains_and_ages_last_good_fix(self):
        calls, recovered, lost = [], Event(), Event()
        good = fix()
        def reader(args):
            calls.append(args.serial_port)
            if len(calls) == 2:
                recovered.set()
                return good
            if len(calls) > 2:
                lost.set()
            raise PositionError("GPS has no fix")
        monitor = self.monitor(reader)
        monitor.configure(SETTINGS, read_now=True)
        self.settled(monitor)
        self.assertIsNone(monitor.snapshot()["position"])
        self.assertEqual(monitor.snapshot()["position_settings"]["serial_port"], "COM7")
        self.assertTrue(recovered.wait(3))
        self.settled(monitor)
        self.assertTrue(monitor.snapshot()["position"]["fresh"])
        self.assertTrue(lost.wait(3))
        self.settled(monitor)
        with patch("gmdss_dsc.position.datetime") as clock:
            clock.now.return_value = good.time_utc + timedelta(seconds=31)
            state = monitor.snapshot()
            self.assertFalse(state["position"]["fresh"])
        self.assertEqual(state["position"]["time_utc"], good.as_dict()["time_utc"])
        self.assertEqual(state["position_error"], "GPS has no fix")
        self.assertEqual(calls, ["COM7"] * len(calls))
        monitor.stop_updates()
        count = len(calls)
        self.assertFalse(Event().wait(1.1))
        self.assertEqual(len(calls), count)

    def test_slow_read_does_not_block_snapshot_or_create_overlapping_reads(self):
        entered, release = Event(), Event()
        calls = []
        def reader(args):
            calls.append(args.serial_port)
            entered.set()
            release.wait(3)
            return fix()
        monitor = self.monitor(reader)
        try:
            monitor.configure(SETTINGS, read_now=True)
            self.assertTrue(entered.wait(2))
            self.assertTrue(monitor.snapshot()["position_pending"])
            with self.assertRaisesRegex(ValueError, "already"):
                monitor.configure(SETTINGS, read_now=True)
            # An elapsed interval cannot start a second read while the first waits.
            Event().wait(1.1)
            self.assertEqual(calls, ["COM7"])
        finally:
            release.set()
        self.settled(monitor)

    def test_changed_source_discards_late_result_and_close_stops_retries(self):
        entered, release, new_source = Event(), Event(), Event()
        def reader(args):
            if args.serial_port == "COM7":
                entered.set()
                release.wait(3)
                return fix(51)
            new_source.set()
            raise PositionError("No fix on new port")
        monitor = self.monitor(reader)
        try:
            monitor.configure(SETTINGS, read_now=True)
            self.assertTrue(entered.wait(2))
            monitor.configure(SETTINGS | {"serial_port": "COM8"})
        finally:
            release.set()
        self.assertTrue(new_source.wait(2))
        self.settled(monitor)
        self.assertIsNone(monitor.snapshot()["position"])
        self.assertEqual(monitor.snapshot()["position_settings"]["serial_port"], "COM8")
        monitor.close()
        self.assertFalse(monitor._thread.is_alive())
        with self.assertRaisesRegex(ValueError, "shutting"):
            monitor.configure(SETTINGS)

    def test_close_during_read_discards_result(self):
        entered, release = Event(), Event()
        def reader(args):
            entered.set()
            release.wait(3)
            return fix()
        monitor = self.monitor(reader)
        try:
            monitor.configure(SETTINGS, read_now=True)
            self.assertTrue(entered.wait(2))
            monitor.close(timeout=0)
        finally:
            release.set()
        monitor._thread.join(2)
        self.assertFalse(monitor._thread.is_alive())
        self.assertIsNone(monitor.snapshot()["position"])

    def test_tx_waits_for_existing_read_then_uses_fresh_cache_and_rejects_stale(self):
        entered, release = Event(), Event()
        calls, results, errors = [], [], []
        good = fix()
        def reader(args):
            calls.append(args.serial_port)
            entered.set()
            release.wait(3)
            return good
        monitor = self.monitor(reader)
        settings = SETTINGS | {"position_auto_update": False, "position_timeout": 3}
        monitor.configure(settings, read_now=True)
        def transmit():
            try:
                results.append(monitor.read_for_tx(SimpleNamespace(**settings), Event()))
            except Exception as exc:
                errors.append(exc)
        sender = Thread(target=transmit)
        try:
            self.assertTrue(entered.wait(2))
            sender.start()
            Event().wait(0.1)
            self.assertEqual(calls, ["COM7"])
        finally:
            release.set()
        sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [good])
        self.assertEqual(calls, ["COM7"])
        with patch("gmdss_dsc.position.datetime") as clock:
            clock.now.return_value = good.time_utc + timedelta(seconds=31)
            with self.assertRaisesRegex(PositionError, "stale"):
                monitor.read_for_tx(SimpleNamespace(**settings), Event())
        self.assertEqual(calls, ["COM7", "COM7"])

    def test_tx_cancellation_and_other_source_do_not_reuse_cached_fix(self):
        calls = []
        def reader(args):
            calls.append(args.serial_port)
            return fix()
        monitor = self.monitor(reader)
        monitor.configure(SETTINGS | {"position_auto_update": False}, read_now=True)
        self.settled(monitor)
        cancel = Event()
        cancel.set()
        with self.assertRaisesRegex(PositionError, "cancelled"):
            monitor.read_for_tx(SimpleNamespace(**SETTINGS), cancel)
        monitor.read_for_tx(SimpleNamespace(**(SETTINGS | {"serial_port": "COM8"})), Event())
        self.assertEqual(calls, ["COM7", "COM8"])
        self.assertEqual(monitor.snapshot()["position_settings"]["serial_port"], "COM7")

    def test_invalid_auto_settings_do_not_open_source_or_replace_configuration(self):
        monitor = self.monitor(lambda _: self.fail("Unexpected GPS access"))
        monitor.configure({})
        for changes in ({"position_auto_update": True}, {"position_interval": 0},
                        {"position_interval": float("nan")}, {"position_auto_update": "false"},
                        {"position_timeout": float("inf")}, {"max_fix_age": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                monitor.configure(changes)
        self.assertIsNone(monitor._thread)
        self.assertEqual(monitor.snapshot()["position_settings"]["position_source"], "manual")

    def test_profile_load_restores_automatic_updates_and_manual_profile_stops_them(self):
        read = Event()
        def reader(args):
            read.set()
            return fix()
        service = ReceiverService(fix_reader=reader)
        self.addCleanup(service.close)
        service.configure(ReceiverSettings(), station=dict(name="boat", profile=dict(position=SETTINGS)))
        self.assertTrue(read.wait(2))
        self.settled(service.position)
        self.assertEqual(service.snapshot()["position_settings"]["serial_port"], "COM7")
        service.configure(ReceiverSettings(), station=dict(name="home", profile=dict(position={})))
        self.assertFalse(service.snapshot()["position_settings"]["position_auto_update"])
        self.assertIsNone(service.snapshot()["position"])
