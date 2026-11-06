"""Wire-order examples and error detection; not just inverse-function tests."""
import unittest
from gmdss_dsc.symbols import (
    CharacterError, encode_character, decode_character, encode_mmsi, decode_mmsi,
)


class CharacterTests(unittest.TestCase):
    def test_fixed_wire_vectors(self):
        # M.493 Annex 1 Table 1 conventions: data LSB first, count MSB first.
        for symbol, wire in {0: "0000000111", 1: "1000000110", 120: "0001111011",
                             125: "1011111001", 127: "1111111000",
                             85: "1010101011", 104: "0001011100"}.items():
            bits = tuple(map(int, wire))
            self.assertEqual(encode_character(symbol), bits)
            self.assertEqual(decode_character(bits), symbol)

    def test_all_128_characters_and_every_single_bit_error(self):
        for symbol in range(128):
            bits = encode_character(symbol)
            self.assertEqual(decode_character(bits), symbol)
            for j in range(10):
                corrupted = list(bits)
                corrupted[j] ^= 1
                with self.assertRaises(CharacterError):
                    decode_character(corrupted)

    def test_zero_count_cannot_detect_all_two_bit_errors(self):
        bits = list(encode_character(120))
        bits[0] ^= 1
        bits[3] ^= 1
        self.assertEqual(decode_character(bits), 113)

    def test_bad_inputs(self):
        for value in (-1, 128, 3.5, "120"):
            with self.assertRaises(ValueError):
                encode_character(value)
        for bits in ((0,)*9, (0,)*11, (0,)*9+(2,), (0.0,)*10):
            with self.assertRaises(ValueError):
                decode_character(bits)

    def test_mmsi_wire_and_leading_zeros(self):
        self.assertEqual(encode_mmsi("004634060"), (0,46,34,6,0))
        self.assertEqual(decode_mmsi((21,53,22,0,0)), "215322000")
        self.assertEqual(decode_mmsi((0,46,34,6,0)), "004634060")

    def test_mmsi_validation(self):
        for value in (4634060, "4634060", "0046340600", "abcdefghi", "１２３４５６７８９"):
            with self.assertRaises(ValueError):
                encode_mmsi(value)
        for values in ((0,46,34,6,1), (0,46,34,6), (0,100,34,6,0)):
            with self.assertRaises(ValueError):
                decode_mmsi(values)
