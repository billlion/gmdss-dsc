"""Real rigctld/IC-706 backend with simulated CI-V and RTS/DTR electrical lines.

No physical serial or audio device is opened. The preload shim exists because
Linux pseudo-terminals do not implement modem-control ioctls themselves.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from gmdss_dsc.ham_tx import transmit_once
from gmdss_dsc.rig import RigController, RigSettings
from test_ham_tx import Playback, request


@contextmanager
def ic706():
    import pty
    import tty
    master, slave = pty.openpty()
    tty.setraw(slave)
    stop = Event()
    state = dict(frequency=7100000, mode=0, vfo=1, split=True, duplex=True, commands=[])

    def serve():
        pending = b""
        while not stop.is_set():
            if not select.select([master], [], [], 0.1)[0]:
                continue
            pending += os.read(master, 4096)
            while b"\xfd" in pending:
                frame, pending = pending.split(b"\xfd", 1)
                start = frame.find(b"\xfe\xfe")
                if start < 0:
                    continue
                frame = frame[start:] + b"\xfd"
                if len(frame) < 6 or frame[2] != 0x48:
                    continue
                payload = frame[4:-1]
                state["commands"].append(payload)
                command, data = payload[0], payload[1:]
                reply = b"\xfb"
                if command == 0x03:
                    digits = f"{state['frequency']:010d}"
                    reply = b"\x03" + bytes(int(digits[i:i+2], 16) for i in range(8, -1, -2))
                elif command == 0x04:
                    reply = bytes([0x04, state["mode"], 1])
                elif command == 0x05 and len(data) == 5:
                    state["frequency"] = int("".join(f"{v:02x}" for v in reversed(data)))
                elif command == 0x06 and data:
                    state["mode"] = data[0]
                elif command == 0x07 and data in (b"\x00", b"\x01"):
                    state["vfo"] = data[0]
                elif command == 0x07 and not data:
                    pass  # select VFO instead of memory mode
                elif payload == b"\x0f\x00":
                    state["split"] = False
                elif payload == b"\x0f\x10":
                    state["duplex"] = False
                else:
                    reply = b"\xfa"  # old radio cannot read split/VOX/PTT
                # CI-V bus echo followed by the radio's addressed response.
                os.write(master, frame + bytes([0xfe, 0xfe, frame[3], 0x48]) + reply + b"\xfd")
    thread = Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield os.ttyname(slave), state
    finally:
        stop.set()
        thread.join(2)
        os.close(master)
        os.close(slave)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("rigctld") and shutil.which("cc"),
                     "Serial-line simulation requires Linux, Hamlib and a C compiler")
class RealSerialPTTTests(unittest.TestCase):
    def test_real_ic706_backend_keys_rts_and_dtr_on_shared_and_separate_ports(self):
        import pty
        import tty
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "serial-lines.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-o", str(library),
                            str(Path(__file__).with_name("serial_line_shim.c")), "-ldl", "-pthread"],
                           check=True, capture_output=True)
            for method in ("RTS", "DTR"):
                for shared in (True, False):
                    with self.subTest(method=method, shared=shared), ic706() as (cat_port, radio):
                        master, slave = pty.openpty()
                        tty.setraw(slave)
                        ptt_port = cat_port if shared else os.ttyname(slave)
                        log = Path(tmp) / f"{method}-{shared}.log"
                        env = dict(os.environ, LD_PRELOAD=str(library), DSC_TEST_CAT_TTY=cat_port,
                                   DSC_TEST_PTT_TTY=ptt_port, DSC_TEST_PTT_LOG=str(log))
                        real_popen = subprocess.Popen
                        def start(*args, **kwargs):
                            return real_popen(*args, **kwargs, env=env)
                        rig = RigController()
                        try:
                            with patch("gmdss_dsc.rig.subprocess.Popen", side_effect=start):
                                rig.connect(RigSettings(model=3009, device=cat_port, baud=19200,
                                    civ_address=0x48, allow_ptt=True, ptt_method=method,
                                    ptt_device="" if shared else ptt_port, settle_s=0, timeout_s=2))
                            child = rig._process
                            class CheckOutput(Playback):
                                def play(self, cancel):
                                    assert radio["frequency"] == 7100000 and radio["mode"] == 1
                                    assert radio["vfo"] == 0 and not radio["split"] and not radio["duplex"]
                                    assert rig._command("get_ptt")["PTT"] == "1"
                            result = transmit_once(rig, request().prepare(), playback_factory=CheckOutput)
                            # A front-panel change between bursts must not be
                            # hidden by Hamlib's remembered split/VFO state.
                            radio.update(vfo=1, split=True, duplex=True)
                            result = transmit_once(rig, request().prepare(), playback_factory=CheckOutput)
                            self.assertEqual(result["ptt_confirmation"], "serial_control_line")
                            self.assertEqual(rig._command("get_ptt")["PTT"], "0")
                            values = [line.split()[1:] for line in log.read_text().splitlines()
                                      if line.split()[0] == ptt_port]
                            selected, other = (0, 1) if method == "RTS" else (1, 0)
                            self.assertIn("1", [v[selected] for v in values])
                            self.assertEqual(values[-1][selected], "0")
                            self.assertTrue(all(v[other] == "0" for v in values))
                            self.assertGreaterEqual(radio["commands"].count(b"\x0f\x00"), 4)
                            self.assertGreaterEqual(radio["commands"].count(b"\x0f\x10"), 4)
                            self.assertNotIn(b"\x0f", radio["commands"])
                            self.assertFalse(any(c[0] == 0x1c for c in radio["commands"]))  # no CAT keying
                            rig.close()
                            self.assertIsNotNone(child.poll())
                        finally:
                            rig.close()
                            os.close(master)
                            os.close(slave)
