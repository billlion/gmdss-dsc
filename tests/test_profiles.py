from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from gmdss_dsc.profiles import ProfileStore, StationProfile
from gmdss_dsc.receiver import ReceiverSettings
from gmdss_dsc.rig import RigSettings


class ProfileTests(unittest.TestCase):
    def test_save_roundtrip_keeps_other_stations_and_auto_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp)/"folder"/"stations.toml")
            store.initialize()
            home = store.get("home")
            self.assertEqual(home.rig.civ_address, 0x88)
            self.assertEqual(store.get("tui").rig.remote_id, 11)
            custom = replace(home, radio='Bill’s "home" radio',
                rig=replace(home.rig, executable=r"C:\Program Files\Hamlib\bin\rigctld.exe",
                            ptt_method="RTS", ptt_device="COM7", allow_ptt=True),
                audio=ReceiverSettings(device="USB Audio CODEC", centre_hz=None),
                position=dict(position_source="gpsd", gpsd_host="localhost", gpsd_port=2948))
            store.save("home", custom)
            self.assertEqual(store.get("home"), custom)
            self.assertEqual(store.read()[1], "home")
            self.assertEqual(len(store.read()[0]), 4)
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.initialize()
            self.assertEqual(store.path.read_bytes(), before)

    def test_invalid_profile_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(Path(tmp)/"stations.toml")
            store.initialize()
            before = store.path.read_bytes()
            for profile in (StationProfile(rig=RigSettings(remote_id=100)),
                            StationProfile(frequency_hz=float("nan")),
                            StationProfile(position={"signalk_token": "do not save secrets"})):
                with self.assertRaises(ValueError):
                    store.save("bad", profile)
                self.assertEqual(before, store.path.read_bytes())
            with self.assertRaises(ValueError):
                store.save("../bad", StationProfile())

    def test_unknown_keys_and_malformed_config_are_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"stations.toml"
            store = ProfileStore(path)
            for content in ('version = 1\n[profiles.x.rig]\nserial_typo="COM3"\n', 'version = ['):
                path.write_text(content)
                with self.assertRaisesRegex(ValueError, "Cannot read"):
                    store.read()
