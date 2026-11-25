"""Browser-test server with a WAV capture source; never opens audio hardware."""
import argparse
from pathlib import Path
from unittest.mock import patch
import tempfile
from dataclasses import replace
from datetime import datetime, timezone

from nicegui import ui
from scipy.signal import resample_poly

from gmdss_dsc.cli import position_arguments
from gmdss_dsc.live import AudioBlock
from gmdss_dsc.modem import read_wav
from gmdss_dsc.receiver import ReceiverService
from gmdss_dsc.web import configure_app
from gmdss_dsc.profiles import ProfileStore, StationProfile
from gmdss_dsc.rig import RigSettings, RigController
from gmdss_dsc.ham_tx import TxSettings, transmit_once
from gmdss_dsc.position import PositionError, PositionFix, get_fix
from fake_rigctld import fake_server


def capture(*, sample_rate, stop_event, **kwargs):
    fs, audio = read_wav(Path(__file__).resolve().parents[1] / "examples/gmdss1_clean.wav")
    audio = resample_poly(audio, sample_rate, fs)
    for start in range(0, len(audio), sample_rate // 10):
        if stop_event.wait(0.01):
            return
        yield AudioBlock(start, audio[start:start + sample_rate // 10])
    # Deliver a little silence so the final decoder interval completes.
    import numpy as np
    yield AudioBlock(len(audio), np.zeros(sample_rate))
    while not stop_event.wait(0.1):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    position_arguments(parser)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp, fake_server() as rig, patch("gmdss_dsc.live.capture_blocks", capture):
        class FixtureRig(RigController):
            def connect(self, settings):
                result = super().connect(RigSettings(connection="external", port=rig.server_address[1], settle_s=0))
                self.settings = replace(settings, settle_s=0)
                return result
        class SilentOutput:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def play(self, cancel): cancel.wait(1.5)
        gps_reads = []
        def position_reader(args):
            if args.position_source == "serial" and args.serial_port == "COM-TEST":
                gps_reads.append(True)
                if len(gps_reads) == 1:
                    raise PositionError("Test GPS has no fix yet")
                return PositionFix(50.3, -4.2, datetime.now(timezone.utc), "serial:RMC")
            return get_fix(args)
        service = ReceiverService(rig=FixtureRig(), fix_reader=position_reader, tx_sender=lambda *a, **kw:
                                  transmit_once(*a, **kw, playback_factory=SilentOutput))
        store = ProfileStore(Path(tmp)/"stations.toml")
        store.save("test", StationProfile(radio="Browser test rig", rig=RigSettings(allow_ptt=True, settle_s=0),
                   tx=TxSettings(band_plan="uk", callsign="M0ABC", output_device="Simulated radio")))
        configure_app(service, vars(args), "browser-test-password", store, "test")
        ui.run(host="127.0.0.1", port=args.port, reload=False, show=False,
               fastapi_docs=False, proxy_headers=False)
