"""Hamlib client and an optional process-owned rigctld helper.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
import socket
import subprocess
import tempfile
from threading import RLock
import time


class RigError(RuntimeError):
    """Connection, protocol, or radio operation failed."""


@dataclass(frozen=True)
class RigSettings:
    connection: str = "managed"
    model: int = 1  # Hamlib's hardware-free dummy rig
    device: str = ""
    baud: int = 19200
    remote_id: int | None = None  # Icom marine address, decimal
    civ_address: int | None = None  # Icom amateur CI-V address
    executable: str = "rigctld"
    host: str = "127.0.0.1"
    port: int = 4532
    timeout_s: float = 3.0
    settle_s: float = 0.3
    allow_ptt: bool = False

    def validate(self):
        if type(self.allow_ptt) is not bool:
            raise ValueError("allow_ptt must be true or false")
        if self.allow_ptt and (self.connection != "managed" or self.model not in (1, 3070)):
            raise ValueError("CAT TX currently supports a managed IC-7100 (3070) or dummy (1) only")
        if self.connection not in ("managed", "external"):
            raise ValueError("Rig connection must be managed or external")
        for name, value, low, high in (
            ("Model", self.model, 1, 999999), ("Baud", self.baud, 300, 1000000),
            ("Port", self.port, 1, 65535),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        for name, value, high in (("Remote ID", self.remote_id, 99),
                                  ("CI-V address", self.civ_address, 255)):
            if value is not None and (type(value) is not int or not 1 <= value <= high):
                raise ValueError(f"{name} must be an integer from 1 to {high}")
        if self.remote_id is not None and self.civ_address is not None:
            raise ValueError("Choose a marine Remote ID or a CI-V address, not both")
        for name in ("device", "executable", "host"):
            value = getattr(self, name)
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise ValueError(f"Invalid rig {name}")
        if not self.executable.strip() or not self.host.strip():
            raise ValueError("Enter a rigctld executable and host")
        if self.connection == "managed" and self.model != 1 and not self.device.strip():
            raise ValueError("Enter the radio serial device (COM port or /dev/…)")
        if not (math.isfinite(self.timeout_s) and 0.1 <= self.timeout_s <= 30):
            raise ValueError("Rig timeout must be 0.1–30 seconds")
        if not (math.isfinite(self.settle_s) and 0 <= self.settle_s <= 10):
            raise ValueError("Rig settling time must be 0–10 seconds")

    def command(self, port):
        self.validate()
        command = [self.executable, "-m", str(self.model), "-T", "127.0.0.1",
                   "-t", str(port), "-P", "RIG" if self.allow_ptt else "NONE"]
        configuration = ["cache_timeout=0"]  # readback must query the backend
        if self.device:
            command += ["-r", self.device, "-s", str(self.baud)]
        if self.remote_id is not None:
            configuration.append(f"remoteid={self.remote_id}")
        command += ["-C", ",".join(configuration)]
        if self.civ_address is not None:
            command += ["-c", str(self.civ_address)]
        return command


def usb_dial(frequency_hz, centre_hz):
    """RF FSK centre minus positive audio centre is the USB suppressed carrier."""
    if not math.isfinite(frequency_hz) or not 100000 <= frequency_hz <= 54000000:
        raise ValueError("DSC frequency must be between 100 and 54000 kHz")
    if centre_hz is None or not math.isfinite(centre_hz) or not 85 < centre_hz < 3915:
        raise ValueError("Rig tuning needs an explicit audio centre between 85 and 3915 Hz")
    return round(frequency_hz - centre_hz)


def list_models(executable="rigctld"):
    try:
        result = subprocess.run([executable, "-l"], capture_output=True, text=True,
                                timeout=15, check=False, **_process_options())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RigError(f"Cannot run {executable!r}; install Hamlib or set its full path: {exc}") from exc
    if result.returncode:
        raise RigError(result.stderr.strip() or "rigctld could not list models")
    return result.stdout


def _process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class RigController:
    """One persistent socket; closes only a helper this instance started.

    Callers serialize receiver stop/tune/restart around these operations. The
    lock also prevents interleaving two clients' command/reply transactions.
    """

    def __init__(self):
        self._lock = RLock()
        self._socket = self._reader = self._process = self._log = None
        self.settings = None
        self._status = None
        self._endpoint = None
        self.ptt_uncertain = False
        self._pending = b""

    @property
    def connected(self):
        with self._lock:
            return self._socket is not None

    def snapshot(self):
        with self._lock:
            return dict(self._status) if self._status is not None else None

    def connect(self, settings):
        settings.validate()
        with self._lock:
            if self.ptt_uncertain:
                raise RigError("PTT state is uncertain: disconnect/release before reconnecting")
            if self.connected:
                raise RigError("Disconnect the current radio before connecting another")
            self.settings = settings
            host, port = settings.host, settings.port
            try:
                if settings.connection == "managed":
                    host = "127.0.0.1"
                    with socket.socket() as reservation:
                        reservation.bind((host, 0))
                        port = reservation.getsockname()[1]
                    self._log = tempfile.TemporaryFile(mode="w+b")
                    self._process = subprocess.Popen(
                        settings.command(port), stdin=subprocess.DEVNULL,
                        stdout=self._log, stderr=self._log, **_process_options())
                    deadline = time.monotonic() + max(5, settings.timeout_s * 2)
                    while True:
                        if self._process.poll() is not None:
                            self._log.seek(0)
                            detail = self._log.read(8192).decode(errors="replace").strip()
                            raise RigError(f"rigctld exited during startup: {detail or 'check model, port and baud'}")
                        try:
                            self._socket = socket.create_connection((host, port), timeout=0.2)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise RigError("Timed out waiting for the local rigctld helper")
                            time.sleep(0.05)
                else:
                    self._socket = socket.create_connection((host, port), timeout=settings.timeout_s)
                self._socket.settimeout(settings.timeout_s)
                self._endpoint = (host, port)
                self._reader = self._socket.makefile("rb")
                status = self.read()
                if self._process is not None and self._process.poll() is not None:
                    raise RigError("The local rigctld helper exited while connecting")
                return status
            except (OSError, ValueError, RigError) as exc:
                self.close()
                if isinstance(exc, RigError):
                    raise
                raise RigError(f"Cannot connect to radio: {exc}. Check Hamlib, device, baud and address.") from exc
            except BaseException:
                self.close()
                raise

    def _command(self, command):
        # All callers below supply fixed commands or validated numeric values.
        if self._socket is None:
            raise RigError("Connect a radio first")
        try:
            deadline = time.monotonic() + self.settings.timeout_s
            self._socket.settimeout(self.settings.timeout_s)
            self._socket.sendall(("+\\" + command + "\n").encode("ascii"))
            fields = {}
            for index in range(64):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Radio response timed out")
                # recv with an absolute deadline also bounds a peer dribbling
                # one byte at a time; buffered readline only bounds idle time.
                while b"\n" not in self._pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Radio response timed out")
                    if len(self._pending) > 4096:
                        raise ValueError("Overlong rigctld response")
                    self._socket.settimeout(remaining)
                    chunk = self._socket.recv(4097)
                    if not chunk:
                        raise ValueError("Incomplete rigctld response")
                    self._pending += chunk
                line_bytes, self._pending = self._pending.split(b"\n", 1)
                raw = line_bytes + b"\n"
                if not raw or len(raw) > 4096 or not raw.endswith(b"\n"):
                    raise ValueError("Incomplete or overlong rigctld response")
                line = raw.decode("ascii").strip()
                if line.startswith("RPRT "):
                    code = int(line[5:])
                    if code:
                        raise RigError(f"Hamlib {command.split()[0]} failed (RPRT {code})")
                    return fields
                key, separator, value = line.partition(":")
                if index == 0 and key != command.split()[0]:
                    raise ValueError("Unexpected rigctld response; use the default non-VFO protocol")
                if separator:
                    fields[key] = value.strip()
                elif command == "get_func VOX":
                    fields["Func Status"] = line
            raise ValueError("Too many lines in rigctld response")
        except (OSError, ValueError) as exc:
            # Retain the owned daemon while PTT might be on, so a fresh
            # connection can release it even after a lost key-on reply.
            if self.ptt_uncertain:
                self._drop_transport()
            else:
                self.close()
            raise RigError(f"Lost radio connection: {exc}") from exc

    def read(self):
        with self._lock:
            self._status = None
            frequency = self._command("get_freq")
            mode = self._command("get_mode")
            try:
                dial = float(frequency["Frequency"])
                passband = int(mode["Passband"])
                name = mode["Mode"]
                if not math.isfinite(dial) or dial <= 0 or not name:
                    raise ValueError("Invalid frequency/mode")
            except (KeyError, ValueError) as exc:
                self.close()
                raise RigError("Malformed frequency/mode reply from rigctld") from exc
            self._status = dict(dial_hz=dial, mode=name, passband_hz=passband,
                                confirmed_at_utc=datetime.now(timezone.utc).isoformat(),
                                connection=self.settings.connection,
                                model=self.settings.model if self.settings.connection == "managed" else None)
            return dict(self._status)

    def tune(self, frequency_hz, centre_hz):
        dial = usb_dial(frequency_hz, centre_hz)
        with self._lock:
            if self.ptt_uncertain:
                raise RigError("PTT state is uncertain: tuning is blocked until release is confirmed")
            self._status = None
            self._command("set_mode USB 0")
            self._command(f"set_freq {dial}")
            time.sleep(self.settings.settle_s)
            status = self.read()
            if status["mode"] != "USB" or abs(status["dial_hz"] - dial) > 2:
                self._status = None
                raise RigError(f"Radio readback does not match requested USB / {dial} Hz: {status}")
            status.update(frequency_hz=status["dial_hz"] + centre_hz, centre_hz=centre_hz)
            self._status = status
            return dict(status)

    def close(self):
        with self._lock:
            if self.ptt_uncertain:
                self.release_ptt()
            self._drop_transport()
            if self._process is not None:
                if self._process.poll() is None:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=2)
                self._process = None
            if self._log is not None:
                self._log.close()
                self._log = None

    def _drop_transport(self):
        if self._reader is not None:
            self._reader.close()
        if self._socket is not None:
            self._socket.close()
        self._reader = self._socket = None
        self._status = None
        self._pending = b""

    def require_tx(self):
        if (not self.connected or self.settings is None or not self.settings.allow_ptt
                or self.settings.connection != "managed" or self.settings.model not in (1, 3070)):
            raise RigError("Connect a managed IC-7100 or dummy with CAT PTT enabled first")
        if self.ptt_uncertain:
            raise RigError("PTT state is uncertain: stop the transmitter locally before continuing")

    def check_idle(self):
        with self._lock:
            if self._command("get_ptt").get("PTT") != "0":
                raise RigError("Radio is already transmitting or PTT state is unreadable")

    def prepare_tx(self, frequency_hz, centre_hz):
        """IC-7100 simplex VFO A only; do not infer TX from a marine RX VFO.

        The IC-7100 has RIT but no XIT. Explicit VFO selection avoids memory
        channel TX offsets. Split and DUP must both read back as off.
        """
        self.require_tx()
        with self._lock:
            self.check_idle()
            self._command("set_vfo VFOA")
            self._check_simplex()
            state = self.tune(frequency_hz, centre_hz)
            self._check_simplex()  # tuning must not have enabled an automatic offset
            self.check_idle()
            return state

    def _check_simplex(self):
        if self._command("get_split_vfo").get("Split") != "0":
            raise RigError("Turn SPLIT off on the radio before transmitting")
        if self._command("get_rptr_shift").get("Rptr Shift") != "None":
            raise RigError("Turn DUP/repeater shift off before transmitting")
        # Dummy models arbitrary XIT; the IC-7100 has no XIT facility.
        if self.settings.model == 1 and self._command("get_xit").get("XIT") != "0":
            raise RigError("Turn XIT off before transmitting")
        if self._command("get_func VOX").get("Func Status") != "0":
            raise RigError("Turn VOX off before using CAT PTT")

    def key_ptt(self):
        with self._lock:
            self.ptt_uncertain = True  # BEFORE sending: the acknowledgement may be lost
            self._command("set_ptt 1")
            if self._command("get_ptt").get("PTT") != "1":
                raise RigError("Radio did not confirm PTT on")

    def release_ptt(self):
        """Independent transport, usable after a failed command or audio stall.

        Never terminate the owned helper if release cannot be confirmed.
        No key-on retry is made. Calling code serializes key/release attempts.
        """
        if not self.ptt_uncertain:
            return
        detail = "No recovery endpoint"
        if self._endpoint is not None:
            for _ in range(2):
                client = RigController()
                try:
                    client.settings = self.settings
                    client._socket = socket.create_connection(self._endpoint, timeout=self.settings.timeout_s)
                    client._socket.settimeout(self.settings.timeout_s)
                    client._reader = client._socket.makefile("rb")
                    client._command("set_ptt 0")
                    if client._command("get_ptt").get("PTT") != "0":
                        raise RigError("PTT off readback failed")
                    self.ptt_uncertain = False
                    return
                except (OSError, RigError) as exc:
                    detail = str(exc)
                finally:
                    client.close()
        raise RigError(f"PTT OFF UNCONFIRMED — stop the transmitter locally. "
                       f"Helper retained at {self._endpoint}: {detail}")
