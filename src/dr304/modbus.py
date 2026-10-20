"""Minimal, dependency-light Modbus RTU and Modbus TCP clients.

Only what the D.R304 needs: read holding registers (0x03) and write multiple
registers (0x10).  Written by hand rather than using pymodbus so the hot path
is short and predictable (one request, one exact-length read).
"""

import socket
import struct
import threading
import time
from typing import List, Optional, Sequence


class ModbusError(IOError):
    """Communication failure or Modbus exception response."""


class ModbusException(ModbusError):
    """The device answered with a Modbus exception response (not worth retrying)."""


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _pdu_read(address: int, count: int) -> bytes:
    return struct.pack(">BHH", 0x03, address, count)


def _pdu_write(address: int, values: Sequence[int]) -> bytes:
    regs = [v & 0xFFFF for v in values]
    return struct.pack(">BHHB", 0x10, address, len(regs), 2 * len(regs)) + struct.pack(f">{len(regs)}H", *regs)


def _check_exception(fc: int, body: bytes) -> None:
    if fc & 0x80:
        code = body[0] if body else -1
        raise ModbusException(f"Modbus exception {code} for function 0x{fc & 0x7F:02x}")


class ModbusClient:
    """Common interface. Subclasses implement ``_transact``."""

    def __init__(self, unit: int = 1, timeout: float = 0.2):
        self.unit = unit
        self.timeout = timeout
        self._lock = threading.Lock()

    # public API -------------------------------------------------------------
    def read_holding(self, address: int, count: int) -> List[int]:
        resp = self.transact(_pdu_read(address, count), 2 + 2 * count)
        fc, nbytes = resp[0], resp[1]
        if fc != 0x03 or nbytes != 2 * count:
            raise ModbusError(f"bad read response fc={fc} bytes={nbytes}")
        return list(struct.unpack(f">{count}H", resp[2:2 + nbytes]))

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        resp = self.transact(_pdu_write(address, values), 5)
        if resp[0] != 0x10:
            raise ModbusError(f"bad write response fc={resp[0]}")

    def transact(self, pdu: bytes, resp_pdu_len: int) -> bytes:
        with self._lock:
            return self._transact(pdu, resp_pdu_len)

    def connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _transact(self, pdu: bytes, resp_pdu_len: int) -> bytes:
        raise NotImplementedError

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()


class ModbusRTUClient(ModbusClient):
    """Modbus RTU over a serial port (USB-RS485 adapter).

    The D.R304 has two timing quirks (measured on hardware):

    * it ignores a request that starts less than ~2 ms after the end of its previous reply
      (3 ms is marginal: ~2 % lost), so ``min_gap`` = 4 ms of line silence is enforced;
    * after such an ignored (or corrupted) request it stays deaf until the line has been
      quiet for roughly 80-100 ms, and every further request restarts that wait.
      On a failure the client therefore stays silent for ``recovery`` seconds and retries.

    Its reply latency is ~11 ms, so one 12-register read takes ~13 ms and the serial link
    tops out at roughly 55 reads/s.  Use Ethernet (Modbus TCP) for 120 Hz.
    """

    def __init__(self, port: str, baudrate: int = 115200, unit: int = 1,
                 timeout: float = 0.1, parity: str = "N", stopbits: int = 1,
                 min_gap: float = 0.004, recovery: float = 0.12, retries: int = 1):
        super().__init__(unit, timeout)
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.min_gap = min_gap
        self.recovery = recovery
        self.retries = retries
        self._ser = None
        self._t_quiet = 0.0     # monotonic time of the last byte seen on the line
        self.retried = 0        # requests that needed a recovery + retry

    def connect(self) -> None:
        import serial  # pyserial
        self._ser = serial.Serial(self.port, self.baudrate, bytesize=8, parity=self.parity,
                                  stopbits=self.stopbits, timeout=self.timeout)
        # Low-latency mode cuts the FTDI/CP210x 16 ms buffering on Linux where supported.
        try:
            import fcntl, termios, array  # noqa: E401
            buf = array.array("i", [0] * 32)
            fcntl.ioctl(self._ser.fileno(), termios.TIOCGSERIAL, buf)
            buf[4] |= 0x2000  # ASYNC_LOW_LATENCY
            fcntl.ioctl(self._ser.fileno(), termios.TIOCSSERIAL, buf)
        except Exception:
            pass

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def _transact(self, pdu: bytes, resp_pdu_len: int) -> bytes:
        if self._ser is None:
            raise ModbusError("serial port not open")
        for attempt in range(self.retries + 1):
            try:
                return self._exchange(pdu, resp_pdu_len)
            except ModbusException:
                raise
            except ModbusError:
                if attempt == self.retries:
                    raise
                self.retried += 1
                # Deaf-period recovery: let the line go quiet, then drop any stale bytes.
                time.sleep(self.recovery)
                self._ser.reset_input_buffer()
                self._t_quiet = time.monotonic()

    def _exchange(self, pdu: bytes, resp_pdu_len: int) -> bytes:
        adu = bytes([self.unit]) + pdu
        adu += struct.pack("<H", crc16(adu))
        wait = self.min_gap - (time.monotonic() - self._t_quiet)
        if wait > 0:
            time.sleep(wait)
        self._ser.reset_input_buffer()
        self._ser.write(adu)
        try:
            # Read the header first so an exception response (5 bytes) doesn't stall us.
            head = self._read_exact(3)
            if head[1] & 0x80:
                self._read_exact(2)  # CRC
                _check_exception(head[1], head[2:3])
            # Full frame is unit + PDU + 2 CRC bytes; 3 bytes are already in `head`.
            frame = head + self._read_exact(resp_pdu_len)
        finally:
            self._t_quiet = time.monotonic()
        if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
            raise ModbusError("CRC mismatch")
        if frame[0] != self.unit:
            raise ModbusError(f"response from unit {frame[0]}, expected {self.unit}")
        return frame[1:-2]

    def _read_exact(self, n: int) -> bytes:
        data = self._ser.read(n)
        if len(data) != n:
            raise ModbusError(f"timeout: got {len(data)}/{n} bytes from {self.port}")
        return data


class ModbusTCPClient(ModbusClient):
    """Modbus TCP over Ethernet."""

    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 0.2):
        super().__init__(unit, timeout)
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._tid = 0

    def connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=max(self.timeout, 1.0))
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.timeout)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _transact(self, pdu: bytes, resp_pdu_len: int) -> bytes:
        if self._sock is None:
            raise ModbusError("socket not connected")
        self._tid = (self._tid + 1) & 0xFFFF
        mbap = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit)
        try:
            self._sock.sendall(mbap + pdu)
            while True:
                head = self._recv_exact(7)
                tid, _proto, length, _unit = struct.unpack(">HHHB", head)
                body = self._recv_exact(length - 1)
                if tid == self._tid:
                    break  # drop stale replies from an earlier timed-out request
        except (socket.timeout, OSError) as e:
            raise ModbusError(f"TCP error talking to {self.host}:{self.port}: {e}") from e
        _check_exception(body[0], body[1:])
        return body

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("connection closed by device")
            buf += chunk
        return bytes(buf)


def reconnecting(client: ModbusClient, retries: int = 3, delay: float = 0.2):
    """Try to (re)open a client a few times; returns the client."""
    last = None
    for _ in range(retries):
        try:
            client.close()
            client.connect()
            return client
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay)
    raise ModbusError(f"could not connect: {last}")
