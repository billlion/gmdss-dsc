"""Individual safety test calls and position acknowledgements.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
import math
from numbers import Integral
from .framing import Frame
from .symbols import encode_mmsi, decode_mmsi
from .position import coordinates


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


def encode_position(latitude, longitude):
    """M.493 §8.1.2: quadrant + DDMM + DDDMM, rounded to whole minutes."""
    coordinates(latitude, longitude)
    quadrant = 2*int(math.copysign(1, latitude) < 0)+int(math.copysign(1, longitude) < 0)
    lat_d, lat_m = divmod(math.floor(abs(latitude)*60+0.5), 60)
    lon_d, lon_m = divmod(math.floor(abs(longitude)*60+0.5), 60)
    digits = f"{quadrant}{lat_d:02d}{lat_m:02d}{lon_d:03d}{lon_m:02d}"
    return tuple(int(digits[n:n+2]) for n in range(0, 10, 2))


def decode_position(symbols):
    if len(symbols) != 5 or any(not isinstance(s, Integral) or not 0 <= s <= 99 for s in symbols):
        raise ValueError("Position requires five decimal-pair symbols")
    digits = "".join(f"{s:02d}" for s in symbols)
    quadrant, lat_d, lat_m, lon_d, lon_m = (int(digits[:1]), int(digits[1:3]),
                                          int(digits[3:5]), int(digits[5:8]), int(digits[8:]))
    if quadrant > 3 or lat_m >= 60 or lon_m >= 60:
        raise ValueError("Invalid position quadrant or minutes")
    latitude = (lat_d+lat_m/60)*(-1 if quadrant >= 2 else 1)
    longitude = (lon_d+lon_m/60)*(-1 if quadrant % 2 else 1)
    coordinates(latitude, longitude)
    return latitude, longitude


@dataclass(frozen=True)
class PositionReport:
    """Safety position-request acknowledgement, M.493 Table A1-4.7.

This is the positive reply format (telecommand 121, EOS 122), not a new
broadcast format. No request is sent or answered automatically by this class.
Only known coordinates and known UTC are supported here, with no M.821 suffix.
"""
    source: str
    destination: str
    latitude: float
    longitude: float
    utc_hour: int
    utc_minute: int

    def __post_init__(self):
        encode_mmsi(self.source)
        encode_mmsi(self.destination)
        coordinates(self.latitude, self.longitude)
        if (not isinstance(self.utc_hour, Integral) or not 0 <= self.utc_hour <= 23
                or not isinstance(self.utc_minute, Integral) or not 0 <= self.utc_minute <= 59):
            raise ValueError("Position UTC must be a valid hour and minute")

    def to_frame(self):
        return Frame((120,)+encode_mmsi(self.destination)+(108,)+encode_mmsi(self.source)
                     +(121, 126)+encode_position(self.latitude, self.longitude)
                     +(126, self.utc_hour, self.utc_minute), 122)

    @property
    def dot_bits(self):
        return 20  # Individual acknowledgement: Annex 1 §3.4.

    @classmethod
    def from_frame(cls, frame):
        s = frame.information
        if (len(s) != 22 or s[0] != 120 or s[6] != 108
                or s[12:14] != (121, 126) or s[19] != 126 or frame.eos != 122):
            raise ValueError("Frame is not a supported safety position acknowledgement")
        latitude, longitude = decode_position(s[14:19])
        return cls(decode_mmsi(s[7:12]), decode_mmsi(s[1:6]), latitude, longitude, s[20], s[21])

