"""Browser-test server with a WAV capture source; never opens audio hardware."""
import argparse
from pathlib import Path
from unittest.mock import patch
import tempfile

from nicegui import ui
from scipy.signal import resample_poly

from gmdss_dsc.cli import position_arguments
from gmdss_dsc.live import AudioBlock
from gmdss_dsc.modem import read_wav
from gmdss_dsc.receiver import ReceiverService
from gmdss_dsc.web import configure_app
from gmdss_dsc.profiles import ProfileStore, StationProfile
from gmdss_dsc.rig import RigSettings
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
    service = ReceiverService()
    with tempfile.TemporaryDirectory() as tmp, fake_server() as rig, patch("gmdss_dsc.live.capture_blocks", capture):
        store = ProfileStore(Path(tmp)/"stations.toml")
        store.save("test", StationProfile(radio="Browser test rig", rig=RigSettings(connection="external",
                    port=rig.server_address[1], settle_s=0)))
        configure_app(service, vars(args), "browser-test-password", store, "test")
        ui.run(host="127.0.0.1", port=args.port, reload=False, show=False,
               fastapi_docs=False, proxy_headers=False)
