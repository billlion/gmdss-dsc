import unittest
from gmdss_dsc.calls import GMDSS_1, TestCall
from gmdss_dsc.framing import (
    Frame, FrameError, encode_frame, decode_frame, find_frames, frame_symbols,
)
from gmdss_dsc.symbols import encode_character

# Published GMDSS_1 deinterleaved decode: both formats, EOS, ECC included.
# https://fractaldoctor.github.io/PhosphorDSC/tests.html
PUBLISHED = (120,120,0,46,34,6,0,108,21,53,22,0,0,118,
             126,126,126,126,126,126,126,117,85)


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.frame = GMDSS_1.to_frame()
        self.bits = encode_frame(self.frame)
        self.start = 20+12*10  # dotting then six DX/RX phasing pairs

    def replace(self, j, value, both=False):
        bits = list(self.bits)
        slots = (2*j, 2*j+5) if both else (2*j,)
        for slot in slots:
            p = self.start+slot*10
            bits[p:p+10] = encode_character(value)
        return bits

    def test_published_message_and_checksum(self):
        wire = frame_symbols(self.frame)
        self.assertEqual(wire[12::2][:-2], PUBLISHED)
        self.assertEqual(self.frame.ecc, 85)
        self.assertEqual(TestCall.from_frame(decode_frame(self.bits).frame), GMDSS_1)
        self.assertEqual(len(self.bits), 640)

    def test_phasing_and_closing_layout(self):
        wire = frame_symbols(self.frame)
        self.assertEqual(wire[:16], (125,111,125,110,125,109,125,108,
                                     125,107,125,106,120,105,120,104))
        self.assertEqual(wire[-8:], (117,126,85,126,117,117,117,85))
        for k in range(12, len(wire)-5, 2):
            self.assertEqual(wire[k], wire[k+5])

    def test_acknowledgement_and_dotting(self):
        self.assertEqual(GMDSS_1.dot_bits, 20)
        call = TestCall("004634060", "215322000", True)
        self.assertEqual(call.dot_bits, 20)
        self.assertEqual(call.to_frame().eos, 122)
        self.assertEqual(TestCall.from_frame(decode_frame(encode_frame(call.to_frame())).frame), call)
        request = TestCall("232123456", "215322000")
        self.assertEqual(request.dot_bits, 200)
        self.assertEqual(len(encode_frame(request.to_frame(),dot_bits=request.dot_bits)), 820)

    def test_invalid_copy_recovered_from_repeat(self):
        bits = list(self.bits)
        bits[self.start+4*10] ^= 1
        result = decode_frame(bits)
        self.assertEqual(result.frame, self.frame)
        self.assertEqual(result.recovered_characters, 1)

    def test_both_copies_bad_rejected(self):
        bits = list(self.bits)
        for slot in (4,9):
            bits[self.start+slot*10] ^= 1
        self.assertEqual(find_frames(bits), [])

    def test_valid_but_disagreeing_copies_rejected(self):
        self.assertEqual(find_frames(self.replace(3,47)), [])

    def test_matching_corruption_rejected_by_ecc(self):
        self.assertEqual(find_frames(self.replace(3,47,both=True)), [])

    def test_wrong_ecc_rejected(self):
        self.assertEqual(find_frames(self.replace(22,84,both=True)), [])

    def test_incomplete_or_bad_closing_rejected(self):
        self.assertEqual(find_frames(self.bits[:-1]), [])
        bits = list(self.bits)
        bits[-20] ^= 1
        self.assertEqual(find_frames(bits), [])

    def test_two_bursts_and_unknown_bit_alignment(self):
        results = find_frames((1,0,0)+self.bits+(0,)*17+self.bits)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.frame == self.frame for r in results))
        with self.assertRaises(FrameError):
            decode_frame(self.bits+self.bits)

    def test_semantic_validation_and_other_eos(self):
        raw = Frame(self.frame.information,127)
        self.assertEqual(decode_frame(encode_frame(raw)).frame, raw)
        with self.assertRaises(ValueError):
            TestCall.from_frame(raw)
        for information, eos in (((),117), ((120,117),117), ((120,),117.0), ((120,),1)):
            with self.assertRaises(ValueError):
                Frame(information,eos)
