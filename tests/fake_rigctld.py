"""Small adversarial ERP fixture, never opens a radio or soundcard."""
from contextlib import contextmanager
import socketserver
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
            else:
                code = -4
            reply = f"{verb}: {' '.join(args)}\n{body}RPRT {code}\n".encode()
            if self.server.fragment:
                for byte in reply:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
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
