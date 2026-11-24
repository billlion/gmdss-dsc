"""Small adversarial ERP fixture, never opens a radio or soundcard."""
from contextlib import contextmanager
import socketserver
import time
from threading import Thread


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.dial = 7100000
        self.mode = "LSB"
        self.commands = []
        self.fail = None
        self.drop = None
        self.ignore_tune = False
        self.fragment = False
        self.ptt = 0
        self.split = 0
        self.shift = "None"
        self.xit = 0
        self.vox = 0
        self.lose_key_reply = False
        self.refuse_release = False
        self.key_byte_delay = 0


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            command = line.decode().strip()
            self.server.commands.append(command)
            verb, *args = command.removeprefix("+\\").split()
            if verb == self.server.drop:
                self.wfile.write(b"get_freq:\nFrequency: ")
                return
            body = ""
            code = 0
            if verb == self.server.fail:
                code = -1
            elif verb == "get_freq":
                body = f"Frequency: {self.server.dial}\n"
            elif verb == "get_mode":
                body = f"Mode: {self.server.mode}\nPassband: 2400\n"
            elif verb == "set_freq":
                if not self.server.ignore_tune:
                    self.server.dial = int(args[0])
            elif verb == "set_mode":
                self.server.mode = args[0]
            elif verb == "set_vfo":
                pass
            elif verb == "get_split_vfo":
                body = f"Split: {self.server.split}\nTX VFO: VFOA\n"
            elif verb == "get_rptr_shift":
                body = f"Rptr Shift: {self.server.shift}\n"
            elif verb == "get_xit":
                body = f"XIT: {self.server.xit}\n"
            elif verb == "get_func":
                body = f"{self.server.vox}\n"
            elif verb == "get_ptt":
                body = f"PTT: {self.server.ptt}\n"
            elif verb == "set_ptt":
                if args[0] == "0" and self.server.refuse_release:
                    code = -1
                else:
                    self.server.ptt = int(args[0])
                if args[0] == "1" and self.server.lose_key_reply:
                    return
            else:
                code = -4
            reply = f"{verb}: {' '.join(args)}\n{body}RPRT {code}\n".encode()
            if self.server.fragment or (verb == "set_ptt" and args == ["1"] and self.server.key_byte_delay):
                for byte in reply:
                    try:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    except OSError:
                        return
                    if verb == "set_ptt" and args == ["1"]:
                        time.sleep(self.server.key_byte_delay)
            else:
                self.wfile.write(reply)


@contextmanager
def fake_server():
    server = Server()
    thread = Thread(target=server.serve_forever, kwargs=dict(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
