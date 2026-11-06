"""M.493 Annex 1 §1: ten-bit characters, in transmission order.

Y = 1 (lower HF tone), B = 0 (upper HF tone). The information bits are
LSB first; the count of zero information bits is three bits, MSB first.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from numbers import Integral
from collections.abc import Iterable


class CharacterError(ValueError):
    """The ten-bit character has an invalid zero-count check."""


def encode_character(symbol: int) -> tuple[int, ...]:
    if not isinstance(symbol, Integral) or not 0 <= symbol < 128:
        raise ValueError("A DSC symbol must be an integer in 0..127")
    value = int(symbol)
    zeros = 7 - value.bit_count()
    return tuple((value >> i) & 1 for i in range(7)) + tuple(
        (zeros >> i) & 1 for i in (2, 1, 0)
    )


def validate_bits(bits: Iterable[int]) -> tuple[int, ...]:
    result = tuple(bits)
    if any(not isinstance(b, Integral) or b not in (0, 1) for b in result):
        raise ValueError("Bits must be integer 0 or 1 values")
    return tuple(int(b) for b in result)


def decode_character(bits: Iterable[int]) -> int:
    b = validate_bits(bits)
    if len(b) != 10:
        raise ValueError("A DSC character has exactly ten bits")
    value = sum(b[i] << i for i in range(7))
    check = 4*b[7] + 2*b[8] + b[9]
    if check != 7 - sum(b[:7]):
        raise CharacterError("Character zero-count check failed")
    return value


def encode_symbols(symbols: Iterable[int]) -> tuple[int, ...]:
    return tuple(bit for symbol in symbols for bit in encode_character(symbol))


def encode_mmsi(mmsi: str) -> tuple[int, ...]:
    if not isinstance(mmsi, str) or len(mmsi) != 9 or not mmsi.isascii() or not mmsi.isdigit():
        raise ValueError("An MMSI must be a string of exactly nine ASCII digits")
    padded = mmsi + "0"
    return tuple(int(padded[i:i+2]) for i in range(0, 10, 2))


def decode_mmsi(symbols: Iterable[int]) -> str:
    values = tuple(symbols)
    if len(values) != 5 or any(not isinstance(s, Integral) or not 0 <= s <= 99 for s in values):
        raise ValueError("An MMSI requires five decimal-pair symbols in 0..99")
    if values[-1] % 10:
        raise ValueError("The tenth MMSI digit must be the padding zero")
    return "".join(f"{s:02d}" for s in values)[:9]
