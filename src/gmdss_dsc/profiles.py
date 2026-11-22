"""Named station profiles shared by the command line and browser server.

SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from threading import RLock

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .receiver import ReceiverSettings
from .rig import RigSettings, usb_dial


DSC_FREQUENCIES = [2187500, 4207500, 6312000, 8414500, 12577000, 16804500]
POSITION_KEYS = {"position_source", "latitude", "longitude", "position_time", "serial_port",
                 "baud", "gpsd_host", "gpsd_port", "signalk_url", "position_timeout", "max_fix_age"}


def config_path():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "gmdss-dsc" / "stations.toml"


@dataclass(frozen=True)
class StationProfile:
    radio: str = "Hamlib dummy"
    rig: RigSettings = field(default_factory=RigSettings)
    audio: ReceiverSettings = field(default_factory=ReceiverSettings)
    frequency_hz: float = 2187500
    presets_hz: list = field(default_factory=lambda: list(DSC_FREQUENCIES))
    position: dict = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.radio, str) or len(self.radio) > 200:
            raise ValueError("Radio description must be at most 200 characters")
        self.rig.validate()
        self.audio.validate()
        if self.audio.device is not None and type(self.audio.device) not in (str, int):
            raise ValueError("Audio device must be a number or a name")
        usb_dial(self.frequency_hz, self.audio.centre_hz or 1700)
        if not isinstance(self.presets_hz, list) or len(self.presets_hz) > 50:
            raise ValueError("Use at most 50 frequency presets")
        for frequency in self.presets_hz:
            usb_dial(frequency, self.audio.centre_hz or 1700)
        if not isinstance(self.position, dict) or self.position.keys() - POSITION_KEYS:
            raise ValueError("Unknown position setting in profile")
        for key, value in self.position.items():
            if value is not None and type(value) not in (str, int, float):
                raise ValueError(f"Invalid position setting: {key}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"Non-finite position setting: {key}")

    def to_dict(self):
        data = asdict(self)
        # TOML has no null; preserve the deliberate auto-centre setting.
        if data["audio"]["centre_hz"] is None:
            data["audio"]["centre_hz"] = "auto"
        return data

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        try:
            rig = RigSettings(**data.pop("rig", {}))
            audio = dict(data.pop("audio", {}))
            if audio.get("centre_hz") == "auto":
                audio["centre_hz"] = None
            profile = cls(rig=rig, audio=ReceiverSettings(**audio), **data)
            profile.validate()
            return profile
        except (TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid station profile: {exc}") from exc


class ProfileStore:
    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else config_path()
        self._lock = RLock()

    def read(self):
        with self._lock:
            if not self.path.exists():
                return {}, None
            try:
                with self.path.open("rb") as stream:
                    data = tomllib.load(stream)
                if data.keys() - {"version", "default_profile", "profiles"} or data.get("version") != 1:
                    raise ValueError("Expected station configuration version = 1")
                profiles = {}
                for name, value in data.get("profiles", {}).items():
                    self._name(name)
                    profiles[name] = StationProfile.from_dict(value)
                default = data.get("default_profile")
                if default is not None and default not in profiles:
                    raise ValueError("Default profile does not exist")
                return profiles, default
            except (TypeError, AttributeError, ValueError) as exc:
                raise ValueError(f"Cannot read {self.path}: {exc}") from exc

    def get(self, name):
        profiles, _ = self.read()
        if name not in profiles:
            raise ValueError(f"No profile {name!r} in {self.path}; use 'dsc profiles init' or save one in the GUI")
        return profiles[name]

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Profile name: use 1–64 letters, digits, underscores or hyphens")

    def save(self, name, profile):
        self._name(name)
        profile.validate()
        with self._lock:
            profiles, _ = self.read()
            profiles[name] = profile
            self._write(profiles, name)

    def initialize(self):
        with self._lock:
            if self.path.exists():
                raise ValueError(f"Configuration already exists: {self.path}; it has not been replaced")
            profiles = dict(
                dummy=StationProfile(),
                home=StationProfile(radio="Icom IC-7100", rig=RigSettings(
                    model=3070, device="COM3", baud=19200, civ_address=0x88),
                    audio=ReceiverSettings(device="USB Audio CODEC")),
                tui=StationProfile(radio="Icom IC-M801E (M802 backend; needs hardware test)",
                    rig=RigSettings(model=30002, device="/dev/ttyUSB0", baud=4800, remote_id=11),
                    position=dict(position_source="gpsd", gpsd_host="127.0.0.1", gpsd_port=2947)),
                external=StationProfile(radio="Existing rigctld", rig=RigSettings(connection="external")),
            )
            self._write(profiles, "dummy")

    def _write(self, profiles, default):
        lines = ["# DSC station profiles. Device names/ports belong to the server computer.",
                 "version = 1", f"default_profile = {json.dumps(default)}", ""]
        for name, profile in profiles.items():
            data = profile.to_dict()
            prefix = f"profiles.{json.dumps(name)}"
            for section, values in [(prefix, {k: v for k, v in data.items() if not isinstance(v, dict)})] + [
                    (prefix + "." + key, value) for key, value in data.items() if isinstance(value, dict)]:
                lines.append(f"[{section}]")
                for key, value in values.items():
                    if value is not None:
                        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False, allow_nan=False)}")
                lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".stations-", suffix=".toml", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write("\n".join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
