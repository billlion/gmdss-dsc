import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np

from gmdss_dsc.calls import GMDSS_1
from gmdss_dsc.cli import main
from gmdss_dsc.framing import encode_frame
from gmdss_dsc.live import AudioInputError
from gmdss_dsc.modem import modulate, demodulate, read_wav
from gmdss_dsc.transmit import identification, morse_audio, with_identification, play_audio


class CWTests(unittest.TestCase):
    def test_text_validation_and_standard_timing(self):
        self.assertEqual(identification("m0abc/mm", "io70xx"), "DE M0ABC/MM IO70XX")
        for call, grid in (("", None), ("ABC", None), ("M0?", None),
                           (None, "IO70"), ("M0ABC", "ZZ99AA")):
            with self.assertRaises(ValueError):
                identification(call, grid)
        # E, inter-letter gap, T, inter-word gap, E = 1+3+3+7+1 units.
        audio = morse_audio("ET E", sample_rate=8000, wpm=20)
        self.assertEqual(len(audio), 15*480)
        self.assertEqual(audio[0], 0)
        self.assertEqual(audio[-1], 0)
        self.assertLessEqual(np.max(np.abs(audio)), 0.6)
        for speed in (0, 100, float("nan")):
            with self.assertRaises(ValueError):
                morse_audio("E", wpm=speed)

    def test_cw_prefix_preserves_complete_dsc_decode(self):
        original = modulate(encode_frame(GMDSS_1.to_frame(), dot_bits=20))
        audio = with_identification(original, "DE M0ABC IO70XX", sample_rate=8000, centre_hz=1700)
        np.testing.assert_array_equal(audio[-len(original):], original)
        result = demodulate(audio, 8000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].decoded.frame, GMDSS_1.to_frame())


class FakeOutput:
    def __init__(self, failure=None):
        self.failure = failure
        self.blocks = []
        self.aborted = False
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def abort(self): self.aborted = True
    def write(self, block):
        self.blocks.append(block.copy())
        if self.failure == "interrupt": raise KeyboardInterrupt
        return self.failure == "underflow"


def backend(output):
    return SimpleNamespace(query_devices=lambda *a: {"default_samplerate": 48000, "max_output_channels": 2},
                           check_output_settings=lambda **kw: None,
                           OutputStream=lambda **kw: output, PortAudioError=RuntimeError)


class PlaybackTests(unittest.TestCase):
    def test_rate_conversion_and_right_channel_preserve_message(self):
        audio = modulate(encode_frame(GMDSS_1.to_frame(), dot_bits=20))
        output = FakeOutput()
        with patch("gmdss_dsc.transmit.load_sounddevice", return_value=backend(output)):
            play_audio(audio, 8000, device=3, channel=2)
        blocks = np.concatenate(output.blocks)
        self.assertEqual(len(blocks), len(audio)*6)
        self.assertTrue(np.all(blocks[:, 0] == 0))
        self.assertEqual(demodulate(blocks[:, 1], 48000)[0].decoded.frame, GMDSS_1.to_frame())
        self.assertTrue(output.closed)
        self.assertFalse(output.aborted)

    def test_interruption_and_underflow_abort_without_repeating(self):
        for failure, error in (("interrupt", KeyboardInterrupt), ("underflow", AudioInputError)):
            output = FakeOutput(failure)
            with patch("gmdss_dsc.transmit.load_sounddevice", return_value=backend(output)):
                with self.assertRaises(error):
                    play_audio(np.zeros(8000), 8000)
            self.assertTrue(output.aborted)
            self.assertTrue(output.closed)
            self.assertEqual(len(output.blocks), 1)


class EncodePositionCLITests(unittest.TestCase):
    def test_position_wav_with_cw_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = str(Path(tmp)/"position.wav")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["encode-position", wav, "--source", "215322000", "--destination", "004634060",
                      "--latitude", "50.201", "--longitude", "-4.501", "--callsign", "M0ABC",
                      "--locator", "auto"])
            encoded = json.loads(output.getvalue())
            self.assertTrue(encoded["cw_text"].startswith("DE M0ABC IO70"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["decode", wav]), 0)
            decoded = json.loads(output.getvalue())
            self.assertEqual(len(decoded), 1)
            self.assertEqual(decoded[0]["position_report"], encoded["position_report"])
            self.assertEqual(decoded[0]["position_report"]["latitude"], 50.2)
            self.assertEqual(decoded[0]["position_report"]["longitude"], -4.5)
            self.assertIsNone(decoded[0]["test_call"])
            self.assertFalse(encoded["played"])

    def test_explicit_play_and_optional_wav(self):
        output = io.StringIO()
        with patch("gmdss_dsc.cli.play_audio") as play, contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            main(["encode-test", "--source", "215322000", "--destination", "004634060",
                  "--callsign", "M0ABC", "--play", "--device", "USB", "--output-channel", "2"])
        self.assertEqual(play.call_count, 1)
        self.assertEqual(play.call_args.kwargs, {"device": "USB", "channel": 2})
        self.assertIsNone(json.loads(output.getvalue())["wav"])

    def test_invalid_configuration_never_plays(self):
        cases = [["encode-test", "--device", "3"],
                 ["encode-test", "--play", "--locator", "auto"],
                 ["encode-position", "--play", "--latitude", "nan", "--longitude", "0"],
                 ["encode-position", "--play", "--latitude", "0", "--longitude", "0", "--position-time", "2000-01-01T00:00:00Z"],
                 ["encode-position", "--play", "--latitude", "0", "--longitude", "0", "--position-source", "gpsd"]]
        for args in cases:
            with patch("gmdss_dsc.cli.play_audio") as play, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(args+["--source", "215322000", "--destination", "004634060"])
                self.assertEqual(exc.exception.code, 2)
                play.assert_not_called()

    def test_position_command_has_no_audio(self):
        output = io.StringIO()
        with patch("gmdss_dsc.cli.play_audio") as play, contextlib.redirect_stdout(output):
            main(["position", "--latitude", "52", "--longitude", "0"])
        self.assertEqual(json.loads(output.getvalue())["locator"], "JO02AA")
        play.assert_not_called()
