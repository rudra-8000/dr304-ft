"""D.R304 six-axis F/T transmitter register map.

The vendor ships no protocol document. Everything below was recovered from the
vendor's own Windows tools (``D.R304调试工具.exe`` / ``D.R304高速采集系统``) by
extracting the LabVIEW VIs and reading their constants, plus the parameter
table shown in the debugging tool.  See ``docs/PROTOCOL_NOTES.md``.

Conventions
-----------
* Addresses are Modbus *holding register* addresses (function 0x03 to read,
  0x10 to write), zero based, as used on the wire.
* Every value on this device spans two registers.  Word order is configurable on the
  device (register 1592, tens digit): 0 = high word first (default), 1 = low word first.
* Parameters and the mV/V block are signed 32-bit integers.
* The live force/torque block (0x0400) is IEEE-754 **float32** in physical units (confirmed
  on hardware: e.g. 0x429c8f5c 0x... = 78.28 N).  ``force_decimals``/``torque_decimals`` are
  only the number of significant decimals the device rounds to.
"""

from dataclasses import dataclass
from typing import Dict, Optional

# --- live data -------------------------------------------------------------
REG_FORCE = 0x0400          # 1024: Fx, Fy, Fz, Mx, My, Mz as 6 x float32 (12 regs)
REG_FORCE_COUNT = 12
REG_MVV = 0x0A50            # 2640: raw bridge signal (mV/V) for the 6 channels, 6 x int32
REG_MVV_COUNT = 12

# --- command register ------------------------------------------------------
REG_COMMAND = 0x0A20        # 2592
CMD_SAVE = 40               # persist parameters to flash
CMD_FACTORY_RESET = 30      # restore factory parameters (vendor tool then sends CMD_SAVE)

# --- network ---------------------------------------------------------------
REG_IP_HI = 1598            # ip[0] * 1000 + ip[1]
REG_IP_LO = 1600            # ip[2] * 1000 + ip[3]
REG_IP_PORT = 1602          # TCP port (default 502)

CHANNELS = ("fx", "fy", "fz", "mx", "my", "mz")

FORCE_UNITS = {1: "t", 2: "kg", 3: "g", 4: "kN", 5: "N", 6: "lb"}
PICKING_RATES_HZ = {0: 6.25, 1: 12.5, 2: 25, 3: 50, 4: 100, 5: 200, 6: 400, 7: 800}
BAUD_RATES = {0: 2400, 1: 4800, 2: 9600, 3: 19200, 4: 38400, 5: 57600, 6: 115200}
RS485_PROTOCOLS = {0: "off", 1: "modbus-rtu", 2: "autosend"}
ETH_PROTOCOLS = {0: "off", 1: "modbus-tcp", 2: "das-tcp"}


@dataclass(frozen=True)
class Param:
    name: str
    address: int
    decimals: int = 0       # stored as int(value * 10**decimals)
    writable: bool = True   # exposed for writing by the CLI
    doc: str = ""


PARAMS: Dict[str, Param] = {p.name: p for p in [
    Param("manual_zero_range", 1538, doc="Force values within this range can be manually zeroed"),
    Param("zero_tracking_range", 1540, doc="Values within this range auto-zero"),
    Param("clear_mode", 1542, doc="units digit: 1=zero on power-up; tens digit: 1=zero tracking on"),
    Param("stability_condition", 1544, doc="0-1000 d (default 50). 0 for fastest response"),
    Param("shock_filter", 1546, doc="0-3 (0 = off)"),
    Param("filter_depth", 1548, doc="0-1200; larger = smoother but more lag. 1 for fastest response"),
    Param("force_unit", 1556, doc="1=t 2=kg 3=g 4=kN 5=N 6=lb"),
    Param("force_decimals", 1558, doc="0-5, decimal places of Fx/Fy/Fz"),
    Param("torque_decimals", 1560, doc="0-5, decimal places of Mx/My/Mz"),
    Param("picking_rate", 1562, doc="ADC rate 0-7 = 6.25/12.5/25/50/100/200/400/800 Hz"),
    Param("rs485_protocol", 1588, doc="0=off 1=Modbus RTU 2=AutoSend"),
    Param("rs485_baud", 1590, doc="0-6 = 2400/4800/9600/19200/38400/57600/115200"),
    Param("rs485_format", 1592, doc="units 0-5 = n80 n81 e81 e82 o81 o82; tens 0=high word first 1=low word first"),
    Param("packet_interval", 1594, decimals=3, doc="RS485 AutoSend interval, s (default 0.010)"),
    Param("rs485_address", 1596, doc="Modbus slave id 0-255 (default 1)"),
    Param("eth_protocol", 1604, doc="0=off 1=Modbus TCP 2=DAS_TCP (vendor GUI)"),
    Param("eth_address", 1612, doc="Affects MAC; must differ between several sensors on one LAN"),
    Param("eth_interval", 1614, decimals=3, doc="DAS_TCP send interval, s (0-60)"),
    Param("k1_fx", 1628, writable=False, doc="Quadratic correction factor Fx"),
    Param("k2_fy", 1648, writable=False, doc="Quadratic correction factor Fy"),
    Param("k3_fz", 1668, writable=False, doc="Quadratic correction factor Fz"),
    Param("k4_mx", 1688, writable=False, doc="Quadratic correction factor Mx"),
    Param("k5_my", 1708, writable=False, doc="Quadratic correction factor My"),
    Param("k6_mz", 1728, writable=False, doc="Quadratic correction factor Mz"),
]}


def describe(name: str, value: float) -> Optional[str]:
    """Human readable meaning of an enum-like parameter value."""
    v = int(value)
    table = {
        "force_unit": FORCE_UNITS,
        "picking_rate": PICKING_RATES_HZ,
        "rs485_baud": BAUD_RATES,
        "rs485_protocol": RS485_PROTOCOLS,
        "eth_protocol": ETH_PROTOCOLS,
    }.get(name)
    if table is not None:
        return str(table.get(v, "?"))
    if name == "rs485_format":
        fmt = ["n80", "n81", "e81", "e82", "o81", "o82"]
        frame = fmt[v % 10] if v % 10 < len(fmt) else "?"
        return f"{frame}, {'low' if (v // 10) % 10 else 'high'} word first"
    return None
