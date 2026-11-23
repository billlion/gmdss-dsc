"""Exercise actual callback playback, resampling, drain and PTT ordering."""
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

from gmdss_dsc.ham_tx import transmit_once
from gmdss_dsc.live import AudioInputError
from gmdss_dsc.modem import demodulate
from fake_rigctld import fake_server
from test_ham_tx import connect_fixture, request


class CallbackStop(Exception): pass
class CallbackAbort(Exception): pass


class OutputBackend:
    PortAudioError = RuntimeError
    CallbackStop, CallbackAbort = CallbackStop, CallbackAbort

    def __init__(self, server, *, underflow=False):
        self.server, self.underflow = server, underflow
        self.queued, self.drain, self.closed = Event(), Event(), Event()
        self.blocks = []
        self.errors = []

    def query_devices(self, *args):
        return {"default_samplerate": 48000, "max_output_channels": 2}

    def check_output_settings(self, **kwargs): pass

    def OutputStream(self, **kwargs):
        assert self.server.ptt == 0  # stream opens before any key-on
        self.kwargs = kwargs
        return self

    def start(self):
        assert self.server.ptt == 1
        def callback_worker():
            try:
                while True:
                    block = np.empty((960, self.kwargs["channels"]), dtype=np.float32)
                    try:
                        self.kwargs["callback"](block, len(block), None, "underflow" if self.underflow else None)
                    except CallbackStop:
                        self.blocks.append(block.copy())
                        self.queued.set()
                        self.drain.wait(3)  # final buffer is still playing
                        break
                    except CallbackAbort:
                        break
                    self.blocks.append(block.copy())
            except Exception as exc:
                self.errors.append(exc)
            finally:
                self.kwargs["finished_callback"]()
        self.thread = Thread(target=callback_worker)
        self.thread.start()

    def abort(self):
        assert self.server.ptt == 0  # unkey BEFORE any potentially blocking driver call

    def close(self):
        self.thread.join(3)
        self.closed.set()


class CallbackOutputTests(unittest.TestCase):
    def test_resampled_right_channel_drains_before_key_off_and_decodes(self):
        with fake_server() as server:
            rig = connect_fixture(server)
            backend = OutputBackend(server)
            message = request(settings=replace(request().settings, output_channel=2)).prepare()
            result, errors = [], []
            def send():
                try: result.append(transmit_once(rig, message))
                except BaseException as exc: errors.append(exc)
            try:
                with patch("gmdss_dsc.transmit.load_sounddevice", return_value=backend):
                    worker = Thread(target=send)
                    worker.start()
                    self.assertTrue(backend.queued.wait(2))
                    self.assertEqual(server.ptt, 1)
                    self.assertTrue(worker.is_alive())
                    backend.drain.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors + backend.errors)
                self.assertTrue(result[0]["ptt_off_confirmed"])
                self.assertTrue(backend.closed.is_set())
                audio = np.concatenate(backend.blocks)
                self.assertTrue(np.all(audio[:, 0] == 0))
                frames = demodulate(audio[:, 1], 48000, centre_hz=1700)
                self.assertEqual(len(frames), 1)
            finally:
                backend.drain.set()
                rig.close()

    def test_callback_underflow_aborts_burst_and_releases_ptt(self):
        with fake_server() as server:
            rig = connect_fixture(server)
            backend = OutputBackend(server, underflow=True)
            try:
                with patch("gmdss_dsc.transmit.load_sounddevice", return_value=backend):
                    with self.assertRaisesRegex(AudioInputError, "underflow"):
                        transmit_once(rig, request().prepare())
                self.assertEqual(server.ptt, 0)
                self.assertTrue(backend.closed.is_set())
                self.assertFalse(backend.errors)
                self.assertEqual(server.commands.count("+\\set_ptt 1"), 1)
            finally:
                rig.close()
