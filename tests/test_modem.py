import os
from pathlib import Path
import tempfile
import unittest
import numpy as np
from scipy.io import wavfile
from gmdss_dsc.calls import GMDSS_1
from gmdss_dsc.framing import encode_frame
from gmdss_dsc.modem import modulate, demodulate, write_wav, read_wav, decode_wav


class ModemTests(unittest.TestCase):
    def setUp(self):
        self.frame = GMDSS_1.to_frame()
        self.bits = encode_frame(self.frame)

    def assert_call(self, audio, fs, **kwargs):
        found = demodulate(audio,fs,**kwargs)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0].decoded.frame,self.frame)
        return found[0]

    def test_phases_centres_and_non_bit_aligned_start(self):
        for fc, phase in ((975,0.0),(1700,1.234),(2200,3.1)):
            with self.subTest(fc=fc):
                audio = modulate(self.bits,centre_hz=fc,phase=phase,leading_s=0.253125)
                result = self.assert_call(audio,8000)
                self.assertAlmostEqual(result.centre_hz,fc,delta=2.0)
                self.assertAlmostEqual(result.data_start_s,1.653125,delta=0.01)

    def test_inverted_polarity(self):
        audio = modulate([1-b for b in self.bits],centre_hz=1700)
        self.assertTrue(self.assert_call(audio,8000,centre_hz=1700).inverted)

    def test_48khz_resampling(self):
        audio = modulate(self.bits,sample_rate=48000,centre_hz=1700,phase=2.4)
        self.assert_call(audio,48000)

    def test_wav_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"test.wav"
            audio = modulate(self.bits,centre_hz=975)
            write_wav(path,audio,8000)
            fs, recovered = read_wav(path)
            self.assertEqual(fs,8000)
            self.assertLess(np.max(np.abs(audio-recovered)),5e-5)
            self.assertEqual(decode_wav(path)[0].decoded.frame,self.frame)

    def test_unsigned_8bit_and_stereo_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"audio.wav"
            wavfile.write(path,8000,np.array([0,128,255],dtype=np.uint8))
            np.testing.assert_allclose(read_wav(path)[1],[-1,0,127/128])
            wavfile.write(path,8000,np.zeros((100,2),dtype=np.int16))
            with self.assertRaises(ValueError):
                read_wav(path)

    def test_empty_silent_and_noise_only(self):
        self.assertEqual(demodulate([],8000),[])
        self.assertEqual(demodulate(np.zeros(8000),8000),[])
        self.assertEqual(demodulate(np.random.default_rng(14).normal(size=64000),8000),[])

    def test_repeated_bursts_remain_separate(self):
        audio = modulate(self.bits)
        result = demodulate(np.r_[audio,audio],8000)
        self.assertEqual(len(result),2)
        self.assertTrue(all(r.decoded.frame == self.frame for r in result))

    def test_invalid_synthesis_inputs(self):
        for kwargs in ({"centre_hz":20},{"sample_rate":8001},{"amplitude":2},
                       {"leading_s":-1},{"phase":float("nan")}):
            with self.assertRaises(ValueError):
                modulate(self.bits,**kwargs)


@unittest.skipUnless(os.environ.get("DSC_REFERENCE_WAV"),
                     "Set DSC_REFERENCE_WAV to the original GMDSS_1 mono WAV")
class RecordedReferenceTests(unittest.TestCase):
    def test_original_gmdss1_recording(self):
        result = decode_wav(os.environ["DSC_REFERENCE_WAV"])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].decoded.frame.information,
                         (120,0,46,34,6,0,108,21,53,22,0,0,118,
                          126,126,126,126,126,126,126))
        self.assertEqual(result[0].decoded.frame.eos,117)
        self.assertEqual(result[0].decoded.frame.ecc,85)
