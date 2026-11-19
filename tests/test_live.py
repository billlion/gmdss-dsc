import contextlib
import builtins
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import resample_poly

from gmdss_dsc.cli import main
from gmdss_dsc.live import (
    AudioBlock, AudioInputError, WindowDecoder, capture_blocks, load_sounddevice,
)
from gmdss_dsc.modem import demodulate, read_wav


class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fs, cls.call = read_wav(
            Path(__file__).resolve().parents[1]/"examples/gmdss1_clean.wav")

    def test_window_boundaries_duplicates_and_absolute_sample_times(self):
        # Both calls cross a decode interval; the second also crosses the
        # boundary of the initial retention window. Same message, new time.
        audio = np.r_[np.zeros(3*self.fs), self.call, np.zeros(self.fs),
                      self.call, np.zeros(14*self.fs)]
        expected = demodulate(audio, self.fs, centre_hz=975)
        decoder = WindowDecoder(self.fs, centre_hz=975, window_s=12, interval_s=2)
        found = []
        # Intentionally unrelated to bit periods, intervals and message starts.
        for start in range(0, len(audio), 1777):
            found.extend(decoder.feed(audio[start:start+1777]))
        found.extend(decoder.flush())
        self.assertEqual(len(expected), 2)
        self.assertEqual(len(found), 2)
        for result, reference in zip(found, expected):
            self.assertEqual(result.decoded.frame, reference.decoded.frame)
            self.assertAlmostEqual(result.data_start_s, reference.data_start_s, delta=0.02)
            self.assertAlmostEqual(result.end_s, reference.end_s, delta=0.02)
        self.assertLessEqual(decoder._buffered, decoder.window_samples)
        self.assertEqual(decoder.flush(), [])

    def test_flush_decodes_final_partial_interval(self):
        decoder = WindowDecoder(self.fs, centre_hz=975, window_s=12, interval_s=4)
        # Message closes after the 4-second check and before the next check.
        self.assertEqual(decoder.feed(self.call), [])
        self.assertEqual(len(decoder.flush()), 1)
        self.assertEqual(decoder.flush(), [])

    def test_default_48khz_capture_replay(self):
        audio = resample_poly(self.call, 6, 1)

        def source(**kwargs):
            self.assertEqual(kwargs["sample_rate"], 48000)
            for start in range(0, len(audio), 4800):
                yield AudioBlock(start, audio[start:start+4800])
            raise KeyboardInterrupt

        out, err = io.StringIO(), io.StringIO()
        with patch("gmdss_dsc.live.capture_blocks", source), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main(["listen", "--centre", "975"])
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ecc"], 85)
        self.assertEqual(records[0]["capture_segment"], 0)

    def test_cli_json_and_reset_after_a_gap(self):
        split = 3*self.fs

        def source(**kwargs):
            yield AudioBlock(0, self.call[:split])
            # A discontinuity: these pieces must not be joined into a call.
            yield AudioBlock(split+100, self.call[split:])
            yield AudioBlock(len(self.call)+100, self.call)
            raise KeyboardInterrupt

        out, err = io.StringIO(), io.StringIO()
        with patch("gmdss_dsc.live.capture_blocks", source), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["listen", "--sample-rate", str(self.fs), "--centre", "975"])
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["ecc_passed"])
        self.assertEqual(records[0]["test_call"]["source"], "215322000")
        self.assertEqual(records[0]["capture_segment"], 1)
        self.assertTrue(records[0]["decoded_at_utc"].endswith("+00:00"))
        self.assertIn("Audio gap", err.getvalue())
        self.assertIn("Stopped", err.getvalue())
        self.assertNotIn("Listening", out.getvalue())

    def test_overflow_block_is_discarded(self):
        def source(**kwargs):
            yield AudioBlock(0, self.call, overflow=True)
            yield AudioBlock(len(self.call), self.call)
            raise KeyboardInterrupt

        out, err = io.StringIO(), io.StringIO()
        with patch("gmdss_dsc.live.capture_blocks", source), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main(["listen", "--sample-rate", str(self.fs), "--centre", "975"])
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["capture_segment"], 1)
        self.assertLess(records[0]["data_start_s"], 2)

    def test_complete_call_just_before_gap_is_preserved(self):
        def source(**kwargs):
            yield AudioBlock(0, self.call)
            yield AudioBlock(len(self.call)+100, np.zeros(self.fs))
            raise KeyboardInterrupt

        out, err = io.StringIO(), io.StringIO()
        with patch("gmdss_dsc.live.capture_blocks", source), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main(["listen", "--sample-rate", str(self.fs), "--centre", "975",
                  "--interval", "4"])
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["capture_segment"], 0)

    def test_capture_selects_channel_copies_buffer_and_closes_stream(self):
        data = np.column_stack((np.zeros(800), np.ones(800))).astype(np.float32)
        closed = []

        class FakeStream:
            active = True

            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                self.kwargs = kwargs

            def __enter__(self):
                self.callback(data, len(data), None,
                              SimpleNamespace(input_overflow=False))
                data[:] = 0  # audio backends can reuse their callback buffer
                return self

            def __exit__(self, *args):
                closed.append(True)

        sd = SimpleNamespace(InputStream=FakeStream, PortAudioError=RuntimeError,
                             check_input_settings=lambda **kwargs: None)
        with patch("gmdss_dsc.live.load_sounddevice", return_value=sd):
            blocks = capture_blocks(device=3, sample_rate=8000, channel=2)
            block = next(blocks)
            blocks.close()
        np.testing.assert_array_equal(block.samples, np.ones(800))
        self.assertEqual(block.first_sample, 0)
        self.assertFalse(block.overflow)
        self.assertEqual(closed, [True])

    def test_missing_optional_dependency_has_actionable_error(self):
        with patch.dict("sys.modules", {"sounddevice": None}):
            with self.assertRaisesRegex(AudioInputError, r"\[audio\]"):
                load_sounddevice()
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as error:
                    main(["devices"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(out.getvalue(), "")

    def test_invalid_settings_fail_before_opening_audio(self):
        for options in (["--channel", "0"], ["--sample-rate", "0"],
                        ["--window", "1", "--interval", "1"],
                        ["--interval", "nan"], ["--centre", "inf"],
                        ["--sample-rate", "2000", "--centre", "1700"]):
            with self.subTest(options=options), \
                    patch("gmdss_dsc.live.capture_blocks") as capture, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["listen"]+options)
                self.assertEqual(error.exception.code, 2)
                capture.assert_not_called()

    def test_audio_backend_initialization_error_is_reported(self):
        original_import = builtins.__import__

        def import_without_audio_server(name, *args, **kwargs):
            if name == "sounddevice":
                raise RuntimeError("PulseAudio: cannot connect to server")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_audio_server):
            with self.assertRaisesRegex(AudioInputError, "Cannot initialize.*PulseAudio"):
                load_sounddevice()

    def test_device_and_auto_centre_options(self):
        with patch("gmdss_dsc.live.listen") as listen:
            self.assertEqual(main(["listen", "--device", "USB Audio", "--centre", "auto"]), 0)
            self.assertEqual(listen.call_args.kwargs["device"], "USB Audio")
            self.assertIsNone(listen.call_args.kwargs["centre_hz"])
            main(["listen", "--device", "3"])
            self.assertEqual(listen.call_args.kwargs["device"], 3)


if __name__ == "__main__":
    unittest.main()
