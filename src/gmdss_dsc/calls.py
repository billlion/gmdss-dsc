"""First semantic message type: individual safety test call / acknowledgement.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from .framing import Frame
from .symbols import encode_mmsi, decode_mmsi


@dataclass(frozen=True)
class TestCall:
    source: str
    destination: str
    acknowledgement: bool = False

    def __post_init__(self):
        encode_mmsi(self.source)
        encode_mmsi(self.destination)
        if not isinstance(self.acknowledgement, bool):
            raise ValueError("acknowledgement must be a boolean")

    def to_frame(self) -> Frame:
        return Frame((120,) + encode_mmsi(self.destination) + (108,)
                     + encode_mmsi(self.source) + (118, 126) + (126,)*6,
                     122 if self.acknowledgement else 117)

    @property
    def dot_bits(self) -> int:
        # Annex 1 §3.4: coast calls and individual acknowledgements use 20.
        return 20 if self.acknowledgement or self.destination.startswith("00") else 200

    @classmethod
    def from_frame(cls, frame: Frame):
        s = frame.information
        if (len(s) != 20 or s[0] != 120 or s[6] != 108 or s[12:14] != (118,126)
                or s[14:] != (126,)*6 or frame.eos not in (117,122)):
            raise ValueError("Frame is not a supported individual safety test call")
        return cls(decode_mmsi(s[7:12]), decode_mmsi(s[1:6]), frame.eos == 122)


# Published GMDSS_1 decode, independent of this implementation.
GMDSS_1 = TestCall(source="215322000", destination="004634060")
