import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from gmdss_dsc.rig import RigController, RigError, RigSettings, usb_dial
from fake_rigctld import fake_server


class RigTests(unittest.TestCase):
    def test_serial_ptt_routes_separate_or_shared_port_and_rx_keeps_line_off(self):
        settings = RigSettings(model=3009, device="COM4", civ_address=0x48,
                               allow_ptt=True, ptt_method="RTS", ptt_device="COM7")
        for method in ("RTS", "DTR"):
            for device in ("COM7", "", "COM4"):
                selected = replace(settings, ptt_method=method, ptt_device=device)
                command = selected.command(40123)
                self.assertEqual(command[command.index("-r") + 1], "COM4")
                self.assertEqual(command[command.index("-p") + 1], device or "COM4")
                self.assertEqual(command[command.index("-P") + 1], method)
                config = command[command.index("-C") + 1]
                self.assertIn("serial_handshake=None", config)
                self.assertIn("ptt_share=0", config)
                self.assertNotIn("_state=", config)
                rx_command = replace(selected, allow_ptt=False).command(40123)
                self.assertEqual(rx_command[rx_command.index("-P") + 1], "NONE")
                self.assertNotIn("-p", rx_command)
                rx_config = rx_command[rx_command.index("-C") + 1]
                if device in ("", "COM4"):
                    self.assertIn(f"{method.lower()}_state=OFF", rx_config)
                else:
                    self.assertNotIn("_state=", rx_config)
        # An old profile keeps the existing CAT path without new options.
        cat = RigSettings(model=3070, device="COM5", allow_ptt=True).command(40123)
        self.assertEqual(cat[cat.index("-P") + 1], "RIG")
        self.assertNotIn("-p", cat)

    def test_invalid_ptt_method_port_or_706_cat_rejected_before_launch(self):
        for settings in (RigSettings(ptt_method="VOX"), RigSettings(ptt_device="COM7\n"),
                         RigSettings(ptt_device=None),
                         RigSettings(model=3009, device="COM4", allow_ptt=True),
                         RigSettings(allow_ptt=True, ptt_method="RTS"),
                         RigSettings(model=30002, device="COM4", allow_ptt=True, ptt_method="RTS")):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.command(40123)

    def settings(self, server):
        return RigSettings(connection="external", port=server.server_address[1], settle_s=0, timeout_s=0.3)

    def test_usb_frequency_and_numeric_validation(self):
        self.assertEqual(usb_dial(2187500, 1700), 2185800)
        self.assertEqual(usb_dial(8414500, 1700), 8412800)
        for frequency, centre in ((float("nan"), 1700), (0, 1700), (2187500, None), (2187500, 85)):
            with self.assertRaises(ValueError):
                usb_dial(frequency, centre)
        for settings in (RigSettings(model=True), RigSettings(model=3070), RigSettings(remote_id=0),
                         RigSettings(civ_address=0x88, remote_id=11), RigSettings(timeout_s=float("nan"))):
            with self.assertRaises(ValueError):
                settings.validate()

    def test_launch_arguments_distinguish_marine_and_civ_and_disable_ptt(self):
        marine = RigSettings(model=30002, device="COM5", baud=4800, remote_id=11,
                             executable="C:\\Program Files\\Hamlib\\bin\\rigctld.exe")
        command = marine.command(40123)
        self.assertEqual(command[0], marine.executable)
        self.assertEqual(command[command.index("-T") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("-P") + 1], "NONE")
        self.assertEqual(command[command.index("-C") + 1], "cache_timeout=0,remoteid=11")
        self.assertNotIn("-c", command)
        amateur = RigSettings(model=3070, device="COM6", civ_address=0x88).command(40124)
        self.assertEqual(amateur[amateur.index("-c") + 1], "136")
        self.assertEqual(amateur[amateur.index("-C") + 1], "cache_timeout=0")

    def test_fragmented_responses_readback_and_external_daemon_survives(self):
        with fake_server() as server:
            server.fragment = True
            rig = RigController()
            self.addCleanup(rig.close)
            status = rig.connect(self.settings(server))
            self.assertEqual(status["mode"], "LSB")
            self.assertEqual(server.commands, ["+\\get_freq", "+\\get_mode"])
            result = rig.tune(2187500, 1700)
            self.assertEqual(result["dial_hz"], 2185800)
            self.assertEqual(result["frequency_hz"], 2187500)
            self.assertEqual(result["mode"], "USB")
            self.assertTrue(all(c.split()[0] in {"+\\get_freq", "+\\get_mode", "+\\set_freq", "+\\set_mode"}
                                for c in server.commands))
            rig.close()
            rig.close()
            self.assertFalse(rig.connected)
            self.assertIsNone(rig.snapshot())
            # Client close does not send quit or terminate another daemon.
            rig.connect(self.settings(server))

    def test_hamlib_error_and_wrong_readback_are_not_success(self):
        with fake_server() as server:
            rig = RigController()
            self.addCleanup(rig.close)
            rig.connect(self.settings(server))
            server.fail = "set_mode"
            with self.assertRaisesRegex(RigError, "RPRT -1"):
                rig.tune(2187500, 1700)
            self.assertNotIn("+\\set_freq 2185800", server.commands)
            self.assertIsNone(rig.snapshot())
            server.fail = None
            server.ignore_tune = True
            with self.assertRaisesRegex(RigError, "readback"):
                rig.tune(2187500, 1700)
            self.assertIsNone(rig.snapshot())

    def test_partial_reply_discards_connection_and_connect_failure_cleans_up(self):
        with fake_server() as server:
            server.drop = "get_freq"
            rig = RigController()
            self.addCleanup(rig.close)
            with self.assertRaises(RigError):
                rig.connect(self.settings(server))
            self.assertFalse(rig.connected)
            self.assertIsNone(rig._reader)
            server.drop = None
            rig.connect(self.settings(server))
            self.assertTrue(rig.connected)

    def test_missing_executable_reports_failure_without_leaking_resources(self):
        rig = RigController()
        with self.assertRaisesRegex(RigError, "Cannot connect"):
            rig.connect(RigSettings(executable="/missing/rigctld-does-not-exist"))
        self.assertIsNone(rig._process)
        self.assertIsNone(rig._log)

    def test_failed_child_startup_captures_error_and_reaps_child(self):
        real_popen = subprocess.Popen
        children = []
        def failed_child(*args, **kwargs):
            child = real_popen([sys.executable, "-c", "import sys; print('Port busy', file=sys.stderr); sys.exit(3)"], **kwargs)
            children.append(child)
            return child
        rig = RigController()
        with patch("gmdss_dsc.rig.subprocess.Popen", side_effect=failed_child):
            with self.assertRaisesRegex(RigError, "Port busy"):
                rig.connect(RigSettings())
        self.assertEqual(children[0].poll(), 3)
        self.assertIsNone(rig._log)


@unittest.skipUnless(shutil.which("rigctld"), "Hamlib executable not installed")
class HamlibIntegrationTests(unittest.TestCase):
    def test_real_managed_dummy_radio_and_process_cleanup(self):
        rig = RigController()
        self.addCleanup(rig.close)
        rig.connect(RigSettings(settle_s=0))
        child = rig._process
        result = rig.tune(2187500, 1700)
        self.assertEqual(result["mode"], "USB")
        self.assertEqual(result["dial_hz"], 2185800)
        self.assertIsNone(child.poll())
        rig.close()
        self.assertIsNotNone(child.poll())
