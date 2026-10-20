"""A fake D.R304 for testing without hardware.

Serves the same register map over Modbus TCP (``SimulatedDR304.serve_tcp``) or
Modbus RTU on a pseudo-terminal (``SimulatedDR304.serve_pty``), producing smooth
sinusoidal wrenches.  ``dr304 sim`` runs it from the command line.
"""

import math
import os
import socket
import struct
import threading
import time
from typing import Dict

from . import registers as R
from .modbus import crc16
from .sensor import float32_to_regs, int32_to_regs


class SimulatedDR304:
    def __init__(self, unit: int = 1, force_decimals: int = 2, torque_decimals: int = 3,
                 low_word_first: bool = False, ip: str = "192.168.1.100"):
        self.unit = unit
        self.low = low_word_first
        self.regs: Dict[int, int] = {}
        self.t0 = time.monotonic()
        params = {"force_unit": 5, "force_decimals": force_decimals, "torque_decimals": torque_decimals,
                  "picking_rate": 7, "rs485_protocol": 1, "rs485_baud": 6, "eth_protocol": 1,
                  "rs485_format": 10 if low_word_first else 1, "packet_interval": 10,
                  "rs485_address": unit, "eth_interval": 1, "stability_condition": 50,
                  "filter_depth": 8}
        for name, p in R.PARAMS.items():
            self._set32(p.address, params.get(name, 0))
        a, b, c, d = (int(x) for x in ip.split("."))
        self._set32(R.REG_IP_HI, a * 1000 + b)
        self._set32(R.REG_IP_LO, c * 1000 + d)
        self._set32(R.REG_IP_PORT, 502)
        self._stop = threading.Event()

    def _set32(self, addr: int, value: int) -> None:
        hi_lo = int32_to_regs([value], self.low)
        self.regs[addr], self.regs[addr + 1] = hi_lo

    def wrench(self):
        t = time.monotonic() - self.t0
        fd = self._get32(R.PARAMS["force_decimals"].address)
        td = self._get32(R.PARAMS["torque_decimals"].address)
        f = [5.0 * math.sin(2 * math.pi * 0.5 * t), 3.0 * math.cos(2 * math.pi * 0.5 * t), -9.81 + 0.5 * math.sin(t)]
        m = [0.10 * math.sin(t), 0.20 * math.cos(t), 0.05 * math.sin(3 * t)]
        return [round(v, fd) for v in f] + [round(v, td) for v in m]

    def wrench_counts(self):
        fd = 10 ** self._get32(R.PARAMS["force_decimals"].address)
        td = 10 ** self._get32(R.PARAMS["torque_decimals"].address)
        w = self.wrench()
        return [int(round(v * fd)) for v in w[:3]] + [int(round(v * td)) for v in w[3:]]

    def _get32(self, addr: int) -> int:
        a, b = self.regs.get(addr, 0), self.regs.get(addr + 1, 0)
        hi, lo = (b, a) if self.low else (a, b)
        v = (hi << 16) | lo
        return v - (1 << 32) if v & 0x80000000 else v

    # PDU handling -------------------------------------------------------------
    def handle_pdu(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        if fc == 0x03:
            addr, count = struct.unpack(">HH", pdu[1:5])
            if R.REG_FORCE <= addr < R.REG_FORCE + R.REG_FORCE_COUNT:
                regs = float32_to_regs(self.wrench(), self.low)
                for i, v in enumerate(regs):
                    self.regs[R.REG_FORCE + i] = v
            if R.REG_MVV <= addr < R.REG_MVV + R.REG_MVV_COUNT:
                for i, v in enumerate(self.wrench_counts()):
                    self._set32(R.REG_MVV + 2 * i, v // 10)
            vals = [self.regs.get(addr + i, 0) for i in range(count)]
            return struct.pack(">BB", 3, 2 * count) + struct.pack(f">{count}H", *vals)
        if fc == 0x10:
            addr, count, _n = struct.unpack(">HHB", pdu[1:6])
            vals = struct.unpack(f">{count}H", pdu[6:6 + 2 * count])
            for i, v in enumerate(vals):
                self.regs[addr + i] = v
            return struct.pack(">BHH", 0x10, addr, count)
        return bytes([fc | 0x80, 1])

    # transports ------------------------------------------------------------------
    def serve_tcp(self, host: str = "127.0.0.1", port: int = 5020) -> threading.Thread:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(4)
        srv.settimeout(0.2)
        self.tcp_port = srv.getsockname()[1]

        def client(conn):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with conn:
                while not self._stop.is_set():
                    try:
                        head = conn.recv(7, socket.MSG_WAITALL)
                        if len(head) < 7:
                            return
                        tid, _p, length, unit = struct.unpack(">HHHB", head)
                        pdu = conn.recv(length - 1, socket.MSG_WAITALL)
                        resp = self.handle_pdu(pdu)
                        conn.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                    except OSError:
                        return

        def accept():
            with srv:
                while not self._stop.is_set():
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    threading.Thread(target=client, args=(conn,), daemon=True).start()

        th = threading.Thread(target=accept, daemon=True)
        th.start()
        return th

    def serve_pty(self) -> str:
        """Modbus RTU on a pseudo-terminal; returns the slave path to open with DR304.serial()."""
        import tty
        master, slave = os.openpty()
        tty.setraw(master)
        tty.setraw(slave)
        path = os.ttyname(slave)
        self._pty_slave = slave  # keep open

        def loop():
            buf = b""
            while not self._stop.is_set():
                try:
                    buf += os.read(master, 256)
                except OSError:
                    return
                while len(buf) >= 8:
                    if buf[0] != self.unit:
                        buf = buf[1:]
                        continue
                    n = 8 if buf[1] == 0x03 else (9 + buf[6] if len(buf) > 6 else 1 << 30)
                    if len(buf) < n:
                        break
                    frame, buf = buf[:n], buf[n:]
                    if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
                        continue
                    resp = bytes([self.unit]) + self.handle_pdu(frame[1:-2])
                    os.write(master, resp + struct.pack("<H", crc16(resp)))

        threading.Thread(target=loop, daemon=True).start()
        return path

    def stop(self) -> None:
        self._stop.set()
