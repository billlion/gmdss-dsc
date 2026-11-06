"""Base-call framing and conservative hard-decision time-diversity decoding.

This is a protocol experiment, not a complete M.493 receiver. Acquisition
requires an exact consecutive three-character phasing pattern; nonconsecutive
phasing acquisition, extensions and uncertain-message inference are not here.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from functools import reduce
from operator import xor
from .symbols import CharacterError, decode_character, encode_symbols, validate_bits

FORMATS = frozenset((102, 112, 114, 116, 120, 123))
EOS = frozenset((117, 122, 127))
PHASING = tuple(v for rx in range(111, 105, -1) for v in (125, rx))
PHASING_BITS = encode_symbols(PHASING)


class FrameError(ValueError):
    """A frame is incomplete, ambiguous, or fails its integrity checks."""


@dataclass(frozen=True)
class Frame:
    """Information symbols with ONE format specifier; EOS and ECC separate.

The raw codec handles the common base-call envelope. It does not validate
the semantics of arbitrary call types. TestCall supplies the first supported
semantic format. Information containing an EOS symbol is not supported.
"""
    information: tuple[int, ...]
    eos: int = 117

    def __post_init__(self):
        values = tuple(self.information)
        encode_symbols(values)  # range/type validation
        if not values or values[0] not in FORMATS:
            raise ValueError("Information must begin with a supported format symbol")
        if any(v in EOS for v in values):
            raise ValueError("EOS symbols cannot appear in this base-call information")
        encode_symbols((self.eos,))
        if self.eos not in EOS:
            raise ValueError("EOS must be 117, 122 or 127")
        object.__setattr__(self, "information", values)

    @property
    def ecc(self) -> int:
        """Seven-bit XOR; format and EOS each contribute exactly once."""
        return reduce(xor, self.information, self.eos)


@dataclass(frozen=True)
class DecodedFrame:
    frame: Frame
    data_start_bit: int
    end_bit: int
    recovered_characters: int


def frame_symbols(frame: Frame) -> tuple[int, ...]:
    """Physical character stream, including phasing but excluding dotting.

DX[k] is followed by RX[k-2]; thus copies of one character begin five
character slots apart (four characters intervene). The initial RX slots
hold phasing symbols 105 and 104. EOS occurs three times in DX and once
in RX; ECC occurs once in each. See M.493 Annex 1 §§1.2, 3, 4, 9, 10.
"""
    fmt = frame.information[0]
    dx = (fmt,) + frame.information + (frame.eos, frame.ecc, frame.eos, frame.eos)
    wire = list(PHASING)
    for k, symbol in enumerate(dx):
        wire.extend((symbol, (105, 104)[k] if k < 2 else dx[k-2]))
    return tuple(wire)


def encode_frame(frame: Frame, *, dot_bits: int = 20) -> tuple[int, ...]:
    if not isinstance(dot_bits, int) or dot_bits not in (20, 200):
        raise ValueError("MF/HF dot pattern must contain 20 or 200 bits")
    return (0, 1) * (dot_bits // 2) + encode_symbols(frame_symbols(frame))


def _decode_body(bits: tuple[int, ...], start: int) -> DecodedFrame:
    if start < 0:
        raise FrameError("Missing start of call content")
    available = min((len(bits)-start)//10, 512)
    wire = []
    for k in range(available):
        try:
            wire.append(decode_character(bits[start+10*k:start+10*k+10]))
        except CharacterError:
            wire.append(None)
    if len(wire) < 8 or wire[1] != 105 or wire[3] != 104:
        raise FrameError("Final RX phasing symbols are missing or invalid")
    recovered = 0

    def choose(j):
        nonlocal recovered
        a_idx, b_idx = 2*j, 2*j+5
        if b_idx >= len(wire):
            raise FrameError("Incomplete diversity pair")
        a, b = wire[a_idx], wire[b_idx]
        if a is None and b is None:
            raise FrameError("Both copies failed the character check")
        if a is not None and b is not None and a != b:
            raise FrameError("Two character-valid copies disagree")
        if a is None or b is None:
            recovered += 1
        return b if a is None else a

    logical = []
    for j in range(len(wire)//2):
        value = choose(j)
        if j == 0 and value not in FORMATS:
            raise FrameError("Invalid format specifier")
        if j == 1 and value != logical[0]:
            raise FrameError("Repeated format specifier disagrees")
        if j >= 2 and value in EOS:
            ecc = choose(j+1)
            frame = Frame(tuple(logical[1:]), value)
            if ecc != frame.ecc:
                raise FrameError("Message ECC failed")
            # The complete closing sequence is needed, including RX ECC.
            if 2*j+7 >= len(wire):
                raise FrameError("Incomplete closing sequence")
            for idx in (2*j+4, 2*j+6):
                if wire[idx] != value:
                    raise FrameError("Closing EOS copies missing or inconsistent")
            return DecodedFrame(frame, start, start+(2*j+8)*10, recovered)
        logical.append(value)
    raise FrameError("No complete closing sequence")


def find_frames(bits) -> list[DecodedFrame]:
    """Find only complete, character-checked, ECC-passing base frames.

An invalid copy may be replaced by its valid repeat. Two valid but
different copies cause rejection; this baseline does not guess using ECC.
"""
    b = validate_bits(bits)
    text = "".join(map(str, b))
    starts = set()
    for j in range(len(PHASING)-2):
        pattern = "".join(map(str, encode_symbols(PHASING[j:j+3])))
        position = text.find(pattern)
        while position >= 0:
            starts.add(position + (len(PHASING)-j)*10)
            position = text.find(pattern, position+1)
    result = []
    for start in sorted(starts):
        try:
            result.append(_decode_body(b, start))
        except (FrameError, ValueError):
            pass
    return result


def decode_frame(bits) -> DecodedFrame:
    frames = find_frames(bits)
    if len(frames) != 1:
        raise FrameError(f"Expected one valid frame, found {len(frames)}")
    return frames[0]
