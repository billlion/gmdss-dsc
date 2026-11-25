import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from gmdss_dsc.cli import main
from gmdss_dsc.ham_tx import (TxSettings, TxRequest, TxCancelled, PTTGuard,
                              check_allocation, transmit_once, UK_BANDS_HZ)
from gmdss_dsc.live import AudioInputError
from gmdss_dsc.modem import demodulate
from gmdss_dsc.position import PositionFix
from gmdss_dsc.profiles import StationProfile, ProfileStore
from gmdss_dsc.rig import RigSettings, RigController, RigError
from gmdss_dsc.receiver import ReceiverService, ReceiverSettings
from fake_rigctld import fake_server


def request(**kwargs):
    return TxRequest(**(dict(kind="test", source="891234567", destination="897654321",
        frequency_hz=7101700, settings=TxSettings(band_plan="uk", callsign="M0ABC",
        output_device="test radio", lead_s=0, tail_s=0)) | kwargs))


def connect_fixture(server, *, model=1):
    rig = RigController()
    rig.connect(RigSettings(connection="external", port=server.server_address[1], timeout_s=0.15, settle_s=0))
    # Simulate the owned helper endpoint, without launching a subprocess/radio.
    rig.settings = replace(rig.settings, connection="managed", model=model, allow_ptt=True)
    return rig


class Playback:
    def __init__(self, *args, **kwargs): self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def play(self, cancel): pass


class AllocationTests(unittest.TestCase):
    def test_form_validation_never_reads_position_or_generates_audio(self):
        with patch("gmdss_dsc.ham_tx.get_fix") as fix, patch("gmdss_dsc.ham_tx.modulate") as audio:
            plan = request(kind="position", locator="auto").validate()
            self.assertEqual(plan["dial_hz"], 7100000)
            fix.assert_not_called()
            audio.assert_not_called()
        with self.assertRaisesRegex(ValueError, "leave it blank"):
            request(locator="auto").validate()
        with self.assertRaises(ValueError): request(source="").validate()
        with self.assertRaises(ValueError): request(destination="123").validate()
        with self.assertRaises(ValueError): request(locator="invalid").validate()

    def test_whole_usb_window_and_all_uk_allocation_edges(self):
        for low, high in UK_BANDS_HZ:
            for dial in (low, high-3000):
                self.assertEqual(check_allocation(dial+1700, 1700, "uk")["dial_hz"], dial)
            for dial in (low-1, high-2999):
                with self.assertRaises(ValueError):
                    check_allocation(dial+1700, 1700, "uk")
        for freq in (2187500, 4207500, 6312000, 8414500, 12577000, 16804500,
                     6000000, 5350000, 5359000, 53000000, float("nan")):
            with self.assertRaises(ValueError):
                check_allocation(freq, 1700, "uk")
        with self.assertRaises(ValueError): check_allocation(7101700, 1700, "")
        with self.assertRaises(ValueError): check_allocation(7101700, 500, "uk")
        with self.assertRaises(RigError): check_allocation(7101700, 1700, "uk", dial_hz=7110000)

    def test_explicit_id_output_plan_and_message_type(self):
        for settings in (TxSettings(), TxSettings(band_plan="uk"),
                         TxSettings(band_plan="uk", callsign="M0ABC")):
            with self.assertRaises(ValueError): request(settings=settings).prepare()
        for kind in ("distress", "raw", "wav"):
            with self.assertRaises(ValueError): request(kind=kind).prepare()
        with self.assertRaises(ValueError): request(locator="auto").prepare()
        with self.assertRaises(ValueError):
            request(settings=replace(request().settings, callsign="M0" + "A"*35), cw_wpm=5).prepare()

    def test_cw_and_dsc_roundtrip_and_fresh_position(self):
        message = request(kind="position", locator="auto",
                          position=dict(latitude=50.3, longitude=-4.2)).prepare()
        self.assertEqual(message.record["cw_text"], "DE M0ABC IO70VH")
        frames = demodulate(message.audio, message.sample_rate, centre_hz=1700)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].decoded.frame.information[0], 120)
        self.assertEqual(message.record["message"]["latitude"], 50.3)


class TxSequenceTests(unittest.TestCase):
    def run_fixture(self, callback):
        with fake_server() as server:
            rig = connect_fixture(server)
            try:
                callback(server, rig)
            finally:
                server.refuse_release = False
                rig.close()

    def test_order_and_unkey_before_audio_close(self):
        def test(server, rig):
            class Output(Playback):
                def __enter__(self):
                    assert server.ptt == 0
                    return self
                def play(self, cancel):
                    assert server.ptt == 1 and server.mode == "USB" and server.dial == 7100000
                def __exit__(self, *args):
                    assert server.ptt == 0
            result = transmit_once(rig, request().prepare(), playback_factory=Output)
            self.assertTrue(result["ptt_off_confirmed"])
            self.assertEqual(server.commands.count("+\\set_ptt 1"), 1)
            self.assertLess(server.commands.index("+\\set_freq 7100000"), server.commands.index("+\\set_ptt 1"))
            self.assertFalse(rig.ptt_uncertain)
        self.run_fixture(test)

    def test_reject_split_dup_xit_vox_active_tx_and_bad_readback(self):
        for key, value in (("split", 1), ("shift", "+"), ("xit", 50), ("vox", 1),
                           ("ptt", 1), ("ignore_tune", True), ("fail", "get_split_vfo")):
            def test(server, rig):
                server.dial = 7000000
                setattr(server, key, value)
                with self.assertRaises(RigError):
                    transmit_once(rig, request().prepare(), playback_factory=Playback)
                self.assertNotIn("+\\set_ptt 1", server.commands)
            self.run_fixture(test)

    def test_unsupported_models_and_disabled_ptt(self):
        for settings in (RigSettings(model=30002, device="COM3", allow_ptt=True),
                         RigSettings(connection="external", allow_ptt=True)):
            with self.assertRaises(ValueError): settings.validate()
        def test(server, rig):
            rig.settings = replace(rig.settings, allow_ptt=False)
            with self.assertRaises(RigError):
                transmit_once(rig, request().prepare(), playback_factory=Playback)
            self.assertNotIn("+\\set_ptt 1", server.commands)
        self.run_fixture(test)

    def test_open_failure_and_stale_fix_never_key(self):
        def test(server, rig):
            class Broken(Playback):
                def __enter__(self): raise AudioInputError("device missing")
            with self.assertRaises(AudioInputError):
                transmit_once(rig, request().prepare(), playback_factory=Broken)
            message = request(kind="position", position=dict(latitude=50, longitude=-4)).prepare()
            message.fix = PositionFix(50, -4, datetime.now(timezone.utc)-timedelta(hours=1), "manual")
            with self.assertRaises(ValueError):
                transmit_once(rig, message, playback_factory=Playback)
            self.assertNotIn("+\\set_ptt 1", server.commands)
        self.run_fixture(test)

    def test_audio_failures_and_ctrl_c_unkey_once_without_replay(self):
        for error in (AudioInputError("underflow"), KeyboardInterrupt()):
            def test(server, rig):
                class Broken(Playback):
                    def play(self, cancel): raise error
                with self.assertRaises(type(error)):
                    transmit_once(rig, request().prepare(), playback_factory=Broken)
                self.assertEqual(server.ptt, 0)
                self.assertEqual(server.commands.count("+\\set_ptt 1"), 1)
            self.run_fixture(test)

    def test_lost_key_ack_uses_fresh_connection_to_release(self):
        def test(server, rig):
            server.lose_key_reply = True
            with self.assertRaises(RigError):
                transmit_once(rig, request().prepare(), playback_factory=Playback)
            self.assertEqual(server.ptt, 0)
            self.assertFalse(rig.connected)
            self.assertFalse(rig.ptt_uncertain)
            self.assertEqual(server.commands.count("+\\set_ptt 1"), 1)
        self.run_fixture(test)

    def test_dribbling_key_reply_has_absolute_deadline_and_releases(self):
        def test(server, rig):
            server.key_byte_delay = 0.05
            started = time.monotonic()
            with self.assertRaises(RigError):
                transmit_once(rig, request().prepare(), playback_factory=Playback)
            self.assertLess(time.monotonic()-started, 1.5)
            self.assertEqual(server.ptt, 0)
        self.run_fixture(test)

    def test_cancel_and_watchdog_release_even_when_playback_stalls(self):
        def test(server, rig):
            for cancel_now in (True, False):
                cancel = Event()
                with PTTGuard(rig, cancel, 0.1):
                    if cancel_now: cancel.set()
                    deadline = time.monotonic()+2
                    while server.ptt and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(server.ptt, 0)
                    self.assertTrue(cancel.is_set())
        self.run_fixture(test)

    def test_failed_release_latches_error_and_close_retries(self):
        def test(server, rig):
            server.refuse_release = True
            with self.assertRaisesRegex(RigError, "PTT OFF UNCONFIRMED"):
                transmit_once(rig, request().prepare(), playback_factory=Playback)
            self.assertTrue(rig.ptt_uncertain)
            self.assertEqual(server.ptt, 1)
            with self.assertRaises(RigError): rig.require_tx()
            commands_before = list(server.commands)
            with self.assertRaises(RigError): rig.tune(2187500, 1700)
            with self.assertRaises(RigError): rig.connect(RigSettings())
            self.assertEqual(server.commands, commands_before)
            with self.assertRaises(RigError): rig.close()
            self.assertTrue(rig.connected)
            server.refuse_release = False
            rig.close()
            self.assertEqual(server.ptt, 0)
        self.run_fixture(test)


class TxCLITests(unittest.TestCase):
    def test_profile_roundtrip_dry_run_has_no_radio_or_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp)/"stations.toml")
            profile = StationProfile(tx=request().settings)
            store.save("test", profile)
            self.assertEqual(store.get("test"), profile)
            output = io.StringIO()
            with patch("gmdss_dsc.cli.RigController") as rig, contextlib.redirect_stdout(output):
                self.assertEqual(main(["transmit-test", "--profile", "test", "--config", str(store.path),
                    "--source", "891234567", "--destination", "897654321", "--frequency-khz", "7101.7", "--dry-run"]), 0)
                rig.assert_not_called()
            record = json.loads(output.getvalue())
            self.assertFalse(record["transmitted"])
            self.assertEqual(record["dial_hz"], 7100000)


class SharedStationTXTests(unittest.TestCase):
    def test_pending_gps_does_not_block_test_call_and_position_tx_can_be_aborted(self):
        entered, release, preparing = Event(), Event(), Event()
        calls, errors = [], []
        def reader(args):
            calls.append(args.serial_port)
            entered.set()
            release.wait(5)
            return PositionFix(50.3, -4.2, datetime.now(timezone.utc), "serial:RMC")
        with fake_server() as server:
            service = ReceiverService(rig=connect_fixture(server), fix_reader=reader,
                tx_sender=lambda *a, **kw: transmit_once(*a, **kw, playback_factory=Playback))
            settings = dict(position_source="serial", serial_port="COM7", position_timeout=3)
            def send_position():
                preparing.set()
                try:
                    service.transmit(request(kind="position", position=settings))
                except Exception as exc:
                    errors.append(exc)
            sender = Thread(target=send_position)
            try:
                service.read_position(settings)
                self.assertTrue(entered.wait(2))
                # A test call contains no position and must not wait for GPS.
                self.assertTrue(service.transmit(request())["ptt_off_confirmed"])
                self.assertTrue(service.snapshot()["position_pending"])
                sender.start()
                self.assertTrue(preparing.wait(2))
                deadline = time.monotonic() + 2
                while not service.snapshot()["tx_active"] and time.monotonic() < deadline:
                    Event().wait(0.01)
                self.assertTrue(service.snapshot()["tx_active"])
                service.abort_tx()
                sender.join(2)
                self.assertFalse(sender.is_alive())
                self.assertEqual(calls, ["COM7"])
                self.assertEqual(len(errors), 1)
                self.assertIn("cancelled", str(errors[0]))
                self.assertEqual(server.ptt, 0)
                # Only the earlier test call keyed PTT, never the waiting TX.
                self.assertEqual(server.commands.count("+\\set_ptt 1"), 1)
            finally:
                release.set()
                if sender.ident is not None:
                    sender.join(2)
                service.close()

    def test_readiness_explains_external_and_ptt_disabled_without_io(self):
        with fake_server() as server:
            service = ReceiverService()
            try:
                self.assertIn("Connect", service.snapshot()["tx_block_reason"])
                service.radio("connect", settings=RigSettings(connection="external", port=server.server_address[1]))
                commands = list(server.commands)
                self.assertIn("existing rigctld supports reception only", service.snapshot()["tx_block_reason"])
                service._rig.settings = replace(service._rig.settings, connection="managed", allow_ptt=False)
                self.assertIn("CAT PTT is disabled", service.snapshot()["tx_block_reason"])
                service._rig.settings = replace(service._rig.settings, allow_ptt=True)
                self.assertEqual(service.snapshot()["tx_block_reason"], "")
                self.assertEqual(commands, server.commands)
            finally:
                service.close()

    def test_rx_closes_before_key_and_restart_uses_new_frequency(self):
        receiving, stopped = Event(), Event()
        runs = []
        def listener(*, on_started, stop_event, **kwargs):
            runs.append(kwargs["centre_hz"])
            on_started()
            receiving.set()
            stop_event.wait(2)
            stopped.set()
        with fake_server() as server:
            rig = connect_fixture(server)
            class Output(Playback):
                def play(self, cancel):
                    assert stopped.is_set()
                    assert server.ptt == 1
            service = ReceiverService(rig=rig, listener=listener, tx_sender=lambda *a, **kw:
                transmit_once(*a, **kw, playback_factory=Output))
            try:
                service.start(ReceiverSettings(centre_hz=975))
                self.assertTrue(receiving.wait(1))
                result = service.transmit(request())
                self.assertTrue(result["ptt_off_confirmed"])
                self.assertEqual(service.snapshot()["rig"]["frequency_hz"], 7101700)
                self.assertFalse(service.snapshot()["tx_active"])
                self.assertEqual(runs, [975, 1700])
            finally:
                service.close()

    def test_other_viewer_can_abort_but_not_tune_or_start_second_job(self):
        entered = Event()
        errors = []
        class Output(Playback):
            def play(self, cancel):
                entered.set()
                cancel.wait(3)
        with fake_server() as server:
            rig = connect_fixture(server)
            service = ReceiverService(rig=rig, tx_sender=lambda *a, **kw:
                transmit_once(*a, **kw, playback_factory=Output))
            def send():
                try: service.transmit(request())
                except Exception as exc: errors.append(exc)
            worker = Thread(target=send)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(service.snapshot()["tx_active"])
                with self.assertRaises(ValueError): service.transmit(request())
                with self.assertRaises(ValueError): service.radio("tune", frequency_hz=2187500, centre_hz=1700)
                with self.assertRaises(ValueError): service.start(ReceiverSettings())
                service.abort_tx()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertIsInstance(errors[0], TxCancelled)
                self.assertEqual(server.ptt, 0)
                self.assertIsNone(service.snapshot()["rig"])
            finally:
                service.close()


@unittest.skipUnless(shutil.which("rigctld"), "Hamlib executable not installed")
class RealHamlibTXTests(unittest.TestCase):
    def test_managed_dummy_ptt_cycle(self):
        rig = RigController()
        try:
            rig.connect(RigSettings(allow_ptt=True, settle_s=0))
            child = rig._process
            result = transmit_once(rig, request().prepare(), playback_factory=Playback)
            self.assertTrue(result["ptt_off_confirmed"])
            self.assertEqual(rig._command("get_ptt")["PTT"], "0")
        finally:
            rig.close()
        self.assertIsNotNone(child.poll())
