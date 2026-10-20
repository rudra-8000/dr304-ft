"""Python SDK for the D.R304 six-axis force/torque transmitter (Linux).

    import dr304

    with dr304.open_stream("tcp://192.168.1.100", rate_hz=30, tare=1.0) as ft:
        ft.start_recording("ep000_ft.csv", meta={"episode": 0})
        print(ft.latest().wrench)          # Wrench(fx, fy, fz, mx, my, mz)  [N, N.m]

    s = dr304.connect("serial:///dev/dr304")   # or direct, no background thread
    print(s.read())

See README.md. The sensor is found from a URI, $DR304_URI, or a sensor.json profile.
"""

from .modbus import ModbusError
from .profile import ConfigError, connect, load_profile, open_stream, parse_uri
from .sensor import DR304, SensorConfig, Wrench
from .stream import FTStream, Sample

__all__ = ["DR304", "FTStream", "Sample", "SensorConfig", "Wrench", "ModbusError", "ConfigError",
           "connect", "open_stream", "load_profile", "parse_uri"]
__version__ = "0.2.0"
