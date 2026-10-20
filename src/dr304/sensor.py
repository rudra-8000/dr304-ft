"""High-level driver for the D.R304 six-axis force/torque transmitter."""

import ipaddress
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import registers as R
from .modbus import ModbusClient, ModbusError, ModbusRTUClient, ModbusTCPClient


class Wrench(NamedTuple):
    fx: float
    fy: float
    fz: float
    mx: float
    my: float
    mz: float

    @property
    def force(self) -> Tuple[float, float, float]:
        return (self.fx, self.fy, self.fz)

    @property
    def torque(self) -> Tuple[float, float, float]:
        return (self.mx, self.my, self.mz)

    def __sub__(self, other: "Wrench") -> "Wrench":
        return Wrench(*(a - b for a, b in zip(self, other)))


def regs_to_int32(regs: List[int], low_word_first: bool = False) -> List[int]:
    out = []
    for i in range(0, len(regs) - 1, 2):
        hi, lo = (regs[i + 1], regs[i]) if low_word_first else (regs[i], regs[i + 1])
        v = (hi << 16) | lo
        out.append(v - (1 << 32) if v & 0x80000000 else v)
    return out


def int32_to_regs(values: List[int], low_word_first: bool = False) -> List[int]:
    regs = []
    for v in values:
        v &= 0xFFFFFFFF
        hi, lo = v >> 16, v & 0xFFFF
        regs += [lo, hi] if low_word_first else [hi, lo]
    return regs


def regs_to_float32(regs: List[int], low_word_first: bool = False) -> List[float]:
    out = []
    for i in range(0, len(regs) - 1, 2):
        hi, lo = (regs[i + 1], regs[i]) if low_word_first else (regs[i], regs[i + 1])
        out.append(struct.unpack(">f", struct.pack(">HH", hi, lo))[0])
    return out


def float32_to_regs(values: List[float], low_word_first: bool = False) -> List[int]:
    regs = []
    for v in values:
        hi, lo = struct.unpack(">HH", struct.pack(">f", v))
        regs += [lo, hi] if low_word_first else [hi, lo]
    return regs


@dataclass
class SensorConfig:
    """Scaling/format info read from the device on connect."""
    force_decimals: int = 2
    torque_decimals: int = 2
    force_unit: str = "N"
    low_word_first: bool = False
    live_format: str = "float32"    # how registers 0x0400.. are encoded: "float32" (hardware) or "int32"
    picking_rate_hz: Optional[float] = None
    extra: Dict[str, float] = field(default_factory=dict)

    @property
    def torque_unit(self) -> str:
        return {"N": "N.m", "kN": "kN.m", "kg": "kg.m", "g": "g.m", "t": "t.m", "lb": "lb.ft?"}.get(
            self.force_unit, self.force_unit + ".m")


class DR304:
    """D.R304 6-axis F/T transmitter.

    >>> s = DR304.tcp("192.168.100.150")     # Ethernet, Ethernet protocol = 1 (Modbus TCP)
    >>> s = DR304.serial("/dev/dr304")       # USB-RS485, 485 protocol = 1 (Modbus RTU)
    >>> with s:
    ...     print(s.read())
    """

    def __init__(self, client: ModbusClient, low_word_first: Optional[bool] = None,
                 force_decimals: Optional[int] = None, torque_decimals: Optional[int] = None,
                 live_format: str = "float32"):
        if live_format not in ("float32", "int32"):
            raise ValueError("live_format must be 'float32' or 'int32'")
        self.client = client
        self.config = SensorConfig(live_format=live_format)
        self._overrides = dict(low_word_first=low_word_first, force_decimals=force_decimals,
                               torque_decimals=torque_decimals)
        self._fscale = 0.01
        self._tscale = 0.01
        self.connected = False

    # construction ------------------------------------------------------------
    @classmethod
    def tcp(cls, host: str, port: int = 502, unit: int = 1, timeout: float = 0.2, **kw) -> "DR304":
        return cls(ModbusTCPClient(host, port, unit, timeout), **kw)

    @classmethod
    def serial(cls, port: str, baudrate: int = 115200, unit: int = 1, timeout: float = 0.1, **kw) -> "DR304":
        return cls(ModbusRTUClient(port, baudrate, unit, timeout), **kw)

    # lifecycle -----------------------------------------------------------------
    def connect(self) -> "DR304":
        self.client.close()
        self.client.connect()
        self.refresh_config()
        self.connected = True
        return self

    def close(self) -> None:
        self.client.close()
        self.connected = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def refresh_config(self) -> SensorConfig:
        """Read decimals, unit and word order so `read()` scales correctly."""
        o = self._overrides
        # The format register tells us the word order, but we must read it before knowing
        # the order. Small values (< 65536) fit in one word, so whichever word is non-zero wins.
        hi, lo = self.client.read_holding(R.PARAMS["rs485_format"].address, 2)
        fmt = hi if hi else lo
        low_first = bool((fmt // 10) % 10) if o["low_word_first"] is None else o["low_word_first"]
        self.config.low_word_first = low_first

        unit, fdec, tdec, rate = self._read_ints(R.PARAMS["force_unit"].address, 4)
        c = self.config
        c.force_unit = R.FORCE_UNITS.get(unit, f"unit{unit}")
        c.force_decimals = fdec if o["force_decimals"] is None else o["force_decimals"]
        c.torque_decimals = tdec if o["torque_decimals"] is None else o["torque_decimals"]
        c.picking_rate_hz = R.PICKING_RATES_HZ.get(rate)
        if not (0 <= c.force_decimals <= 5 and 0 <= c.torque_decimals <= 5):
            raise ModbusError(f"implausible decimals {c.force_decimals}/{c.torque_decimals}; "
                              "try low_word_first=True or pass force_decimals=/torque_decimals=")
        self._fscale = 10.0 ** -c.force_decimals
        self._tscale = 10.0 ** -c.torque_decimals
        return c

    # data ------------------------------------------------------------------------
    def read_raw(self) -> List[float]:
        """Six values exactly as the device reports them (float32, or int32 counts if
        ``live_format="int32"``)."""
        regs = self.client.read_holding(R.REG_FORCE, R.REG_FORCE_COUNT)
        if self.config.live_format == "float32":
            return regs_to_float32(regs, self.config.low_word_first)
        return regs_to_int32(regs, self.config.low_word_first)

    def read(self) -> Wrench:
        """One wrench sample in device units (default N and N.m)."""
        r = self.read_raw()
        fd, td = self.config.force_decimals, self.config.torque_decimals
        if self.config.live_format == "int32":
            f, t = self._fscale, self._tscale
        else:
            f = t = 1.0     # float32 values are already in physical units
        return Wrench(round(r[0] * f, fd), round(r[1] * f, fd), round(r[2] * f, fd),
                      round(r[3] * t, td), round(r[4] * t, td), round(r[5] * t, td))

    def read_mvv_raw(self) -> List[int]:
        """Raw bridge outputs (mV/V counts) for diagnostics."""
        return self._read_ints(R.REG_MVV, 6)

    # parameters ----------------------------------------------------------------
    def get_param(self, name: str) -> float:
        p = R.PARAMS[name]
        v = self._read_ints(p.address, 1)[0]
        return v / 10 ** p.decimals if p.decimals else v

    def get_params(self) -> Dict[str, float]:
        out = {}
        for name in R.PARAMS:
            try:
                out[name] = self.get_param(name)
            except ModbusError:
                out[name] = float("nan")
        return out

    def set_param(self, name: str, value: float, save: bool = False) -> None:
        p = R.PARAMS[name]
        if not p.writable:
            raise ValueError(f"{name} is read-only in this library")
        raw = int(round(value * 10 ** p.decimals))
        self._write_ints(p.address, [raw])
        if save:
            self.save()

    def save(self) -> None:
        """Persist parameters to flash. Power-cycle the transmitter (10 s off) afterwards."""
        self._write_ints(R.REG_COMMAND, [R.CMD_SAVE])

    def get_ip(self) -> Tuple[str, int]:
        a, b, port = self._read_ints(R.REG_IP_HI, 3)
        ip = f"{a // 1000}.{a % 1000}.{b // 1000}.{b % 1000}"
        return ip, port

    def set_ip(self, ip: str, port: int = 502, save: bool = True) -> None:
        o = [int(x) for x in str(ipaddress.IPv4Address(ip)).split(".")]
        if not 0 < port < 65536:
            raise ValueError("port out of range")
        self._write_ints(R.REG_IP_HI, [o[0] * 1000 + o[1]])
        self._write_ints(R.REG_IP_LO, [o[2] * 1000 + o[3]])
        self._write_ints(R.REG_IP_PORT, [port])
        if save:
            self.save()

    # helpers ---------------------------------------------------------------------
    def _read_ints(self, address: int, n: int) -> List[int]:
        regs = self.client.read_holding(address, 2 * n)
        return regs_to_int32(regs, self.config.low_word_first)

    def _write_ints(self, address: int, values: List[int]) -> None:
        self.client.write_registers(address, int32_to_regs(values, self.config.low_word_first))
        time.sleep(0.02)

    def __repr__(self) -> str:
        c = self.client
        where = f"{c.host}:{c.port}" if isinstance(c, ModbusTCPClient) else getattr(c, "port", "?")
        return f"DR304({where}, unit={c.unit})"
