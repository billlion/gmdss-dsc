import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gmdss_dsc.cli import main
from gmdss_dsc.modem import write_wav
from gmdss_dsc.profiles import ProfileStore, StationProfile
from gmdss_dsc.receiver import ReceiverSettings
from gmdss_dsc.rig import RigSettings
from fake_rigctld import fake_server


class CLITests(unittest.TestCase):
    def test_encode_then_decode_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/"call.wav")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["encode-test",path,"--source","215322000",
                             "--destination","004634060","--centre","975"])
            self.assertEqual(code,0)
            self.assertEqual(json.loads(output.getvalue())["ecc"],85)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["decode",path])
            record = json.loads(output.getvalue())[0]
            self.assertEqual(code,0)
            self.assertEqual(record["test_call"]["source"],"215322000")
            self.assertEqual(record["test_call"]["destination"],"004634060")
            self.assertTrue(record["ecc_passed"])

    def test_no_call_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/"empty.wav")
            write_wav(path,[0]*8000,8000)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["decode",path])
            self.assertEqual(code,1)
            self.assertEqual(json.loads(output.getvalue()),[])

    def test_missing_file_exit_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["decode",str(Path(tmp)/"absent.wav")])
            self.assertEqual(error.exception.code,2)

    def test_profile_cli_status_tune_and_listen_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, fake_server() as server:
            path = Path(tmp)/"stations.toml"
            store = ProfileStore(path)
            store.save("test", StationProfile(rig=RigSettings(connection="external",
                port=server.server_address[1], settle_s=0), audio=ReceiverSettings(device="saved device")))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["rig", "status", "--config", str(path), "--profile", "test"]), 0)
            self.assertEqual(json.loads(output.getvalue())["mode"], "LSB")
            self.assertFalse(any("set_" in c for c in server.commands))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["rig", "tune", "--config", str(path), "--profile", "test", "--frequency-khz", "2187.5"])
            self.assertEqual(json.loads(output.getvalue())["dial_hz"], 2185800)
            with patch("gmdss_dsc.live.listen") as listener, contextlib.redirect_stderr(io.StringIO()):
                main(["listen", "--config", str(path), "--profile", "test", "--device", "7", "--centre", "975"])
            self.assertEqual(listener.call_args.kwargs["device"], 7)
            self.assertEqual(listener.call_args.kwargs["centre_hz"], 975)
            self.assertEqual(server.dial, 2186525)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["listen", "--frequency-khz", "2187.5"])
            self.assertEqual(error.exception.code, 2)
