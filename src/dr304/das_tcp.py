"""EXPERIMENTAL: receiver for the vendor's push protocol (Ethernet protocol = 2, "DAS_TCP").

What we know (recovered from the vendor GUI, not from documentation):
  * the device streams fixed 71-byte ASCII frames over TCP port 502;
  * the six channels are 7-character signed integers at byte offsets
    25, 32, 39, 46, 53, 60 (Fx, Fy, Fz, Mx, My, Mz);
  * scaled by the force / torque decimal-point parameters;
  * text commands end in CR, e.g. ``ZERO 000 +0000000 \\r`` (zero) and
    ``WRIT 0008 +0000020\\r`` (set send interval to 20 ms).

The header bytes 0-24 are not decoded.  Prefer Modbus TCP (protocol 1), which is
fully understood.  Use ``dr304 das-dump`` to capture raw frames if you need this.
"""

import re
import socket
import threading
import time
from typing import Optional

from .sensor import SensorConfig, Wrench

FRAME_LEN = 71
FIELD_OFFSETS = (25, 32, 39, 46, 53, 60)
FIELD_LEN = 7
_INT = re.compile(rb"^ *[+-]? *\d+ *$")


def parse_frame(frame: bytes):
    """Return six ints or None if the frame doesn't look like a data frame."""
    if len(frame) < FRAME_LEN:
        return None
    out = []
    for off in FIELD_OFFSETS:
        f = frame[off:off + FIELD_LEN]
        if not _INT.match(f):
            return None
        out.append(int(f.replace(b" ", b"")))
    return out


class DasTcpSensor:
    """Duck-type compatible with DR304 (connect/close/read/config), so FTStream works."""

    def __init__(self, host: str, port: int = 502, force_decimals: int = 2, torque_decimals: int = 2,
                 force_unit: str = "N"):
        self.host, self.port = host, port
        self.config = SensorConfig(force_decimals=force_decimals, torque_decimals=torque_decimals,
                                   force_unit=force_unit)
        self.connected = False
        self.frames = 0
        self.bad = 0
        self._latest: Optional[Wrench] = None
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._cond = threading.Condition()

    def connect(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=2.0)
        self._sock.settimeout(1.0)
        self._stop.clear()
        threading.Thread(target=self._rx, daemon=True).start()
        with self._cond:
            if not self._cond.wait_for(lambda: self._latest is not None, timeout=3.0):
                raise TimeoutError("no DAS_TCP frames received (is Ethernet protocol = 2?)")
        self.connected = True
        return self

    def close(self):
        self._stop.set()
        if self._sock:
            self._sock.close()
            self._sock = None
        self.connected = False

    def read(self) -> Wrench:
        if self._latest is None:
            raise IOError("no data yet")
        return self._latest

    def send_command(self, text: str) -> None:
        self._sock.sendall(text.encode("ascii") + b"\r")

    def _rx(self):
        buf = b""
        locked = False
        fs, ts = 10.0 ** -self.config.force_decimals, 10.0 ** -self.config.torque_decimals
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= FRAME_LEN:
                if not locked:
                    # find an alignment where this and the next frame both parse
                    for k in range(min(FRAME_LEN, len(buf) - FRAME_LEN + 1)):
                        if parse_frame(buf[k:k + FRAME_LEN]) is not None:
                            buf, locked = buf[k:], True
                            break
                    else:
                        buf = buf[-FRAME_LEN:]
                        break
                vals = parse_frame(buf[:FRAME_LEN])
                if vals is None:
                    locked = False
                    self.bad += 1
                    buf = buf[1:]
                    continue
                buf = buf[FRAME_LEN:]
                w = Wrench(vals[0] * fs, vals[1] * fs, vals[2] * fs, vals[3] * ts, vals[4] * ts, vals[5] * ts)
                with self._cond:
                    self._latest = w
                    self.frames += 1
                    self._cond.notify_all()


def dump(host: str, port: int, seconds: float, path: str) -> int:
    """Save the raw byte stream for offline analysis. Returns bytes written."""
    n = 0
    with socket.create_connection((host, port), timeout=3.0) as s, open(path, "wb") as f:
        s.settimeout(0.5)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                break
            f.write(d)
            n += len(d)
    return n
