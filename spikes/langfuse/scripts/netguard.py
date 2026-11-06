"""Record every outbound socket attempt so we can prove 'no network'."""
import socket, logging, sys, threading
ATTEMPTS = []
_orig_connect = socket.socket.connect
_orig_gai = socket.getaddrinfo
def _connect(self, addr):
    ATTEMPTS.append(("connect", addr, threading.current_thread().name))
    return _orig_connect(self, addr)
def _gai(host, *a, **k):
    ATTEMPTS.append(("getaddrinfo", host, threading.current_thread().name))
    return _orig_gai(host, *a, **k)
socket.socket.connect = _connect
socket.getaddrinfo = _gai

class Capture(logging.Handler):
    def __init__(self):
        super().__init__(); self.records = []
    def emit(self, r):
        self.records.append(f"{r.levelname} {r.name}: {r.getMessage()[:160]}")
CAP = Capture()
logging.getLogger().addHandler(CAP)
logging.getLogger().setLevel(logging.DEBUG)
