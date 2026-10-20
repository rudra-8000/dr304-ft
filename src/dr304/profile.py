"""One-call connection helpers: URIs and a per-site profile file.

A *URI* says where the sensor is:

    tcp://192.168.1.100            Ethernet, Modbus TCP, port 502
    tcp://192.168.1.100:502
    serial:///dev/dr304            USB-RS485, Modbus RTU, 115200 baud
    serial:///dev/dr304?baud=auto  try the common baud rates
    /dev/ttyUSB0                   a bare device path means serial
    192.168.1.100                  a bare host means tcp

When no URI is given, `connect()` / `open_stream()` look for one in

    1. the DR304_URI environment variable
    2. a profile file (JSON): $DR304_PROFILE, ./sensor.json, ~/.config/dr304/sensor.json

Profile example (see examples/sensor.example.json):

    {"uri": "serial:///dev/dr304", "unit": 1, "rate_hz": 30}
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .modbus import ModbusError
from .sensor import DR304
from .stream import FTStream

URI_ENV = "DR304_URI"
PROFILE_ENV = "DR304_PROFILE"
DEFAULT_RATE_HZ = 30.0
AUTO_BAUDS = (115200, 19200, 9600, 57600, 38400, 4800, 2400)


class ConfigError(ValueError):
    """The sensor URI or profile file is missing or malformed."""


def profile_paths() -> List[str]:
    paths = []
    if os.environ.get(PROFILE_ENV):
        paths.append(os.path.expanduser(os.environ[PROFILE_ENV]))
    paths.append(os.path.abspath("sensor.json"))
    paths.append(os.path.expanduser("~/.config/dr304/sensor.json"))
    return paths


def load_profile(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the profile dict, or {} if there is none. An explicit `path` must exist."""
    if path is not None:
        candidates = [os.path.expanduser(path)]
    else:
        candidates = [p for p in profile_paths() if os.path.isfile(p)][:1]
    if not candidates:
        return {}
    try:
        with open(candidates[0]) as f:
            prof = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read profile {candidates[0]}: {e}") from e
    if not isinstance(prof, dict):
        raise ConfigError(f"profile {candidates[0]} must be a JSON object")
    prof["_path"] = candidates[0]
    return prof


def parse_uri(uri: str) -> Dict[str, Any]:
    """Split a sensor URI into {'kind': 'tcp'|'serial', ...}."""
    uri = uri.strip()
    if not uri:
        raise ConfigError("empty sensor URI")
    if "://" not in uri:
        uri = ("serial://" + uri) if uri.startswith("/") else ("tcp://" + uri)
    u = urlparse(uri)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    if u.scheme == "tcp":
        if not u.hostname:
            raise ConfigError(f"no host in {uri!r}")
        return {"kind": "tcp", "host": u.hostname, "port": u.port or 502, "query": q}
    if u.scheme == "serial":
        dev = (u.netloc or "") + u.path
        if not dev:
            raise ConfigError(f"no device in {uri!r}")
        return {"kind": "serial", "port": dev, "baud": q.get("baud", "115200"), "query": q}
    raise ConfigError(f"unsupported scheme {u.scheme!r} in {uri!r} (use tcp:// or serial://)")


def resolve(uri: Optional[str] = None, profile: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Work out (uri, profile options): explicit argument, then $DR304_URI, then the profile file."""
    prof = load_profile(profile)
    uri = uri or os.environ.get(URI_ENV) or prof.get("uri")
    if not uri:
        raise ConfigError(
            "no sensor given. Pass a URI (e.g. 'tcp://192.168.1.100' or 'serial:///dev/dr304'), "
            f"set ${URI_ENV}, or create a profile (${PROFILE_ENV}, ./sensor.json, "
            "~/.config/dr304/sensor.json) containing {\"uri\": \"...\"}")
    return uri, prof


def connect(uri: Optional[str] = None, *, unit: Optional[int] = None, timeout: Optional[float] = None,
            profile: Optional[str] = None, **sensor_kw) -> DR304:
    """Open and return a connected `DR304`. Use it directly or as a context manager.

        with dr304.connect("tcp://192.168.1.100") as s:
            print(s.read())
    """
    uri, prof = resolve(uri, profile)
    p = parse_uri(uri)
    unit = int(unit if unit is not None else p["query"].get("unit", prof.get("unit", 1)))
    tmo = timeout if timeout is not None else p["query"].get("timeout", prof.get("timeout"))
    kw: Dict[str, Any] = {"unit": unit}
    if tmo is not None:
        kw["timeout"] = float(tmo)
    if p["kind"] == "tcp":
        sensor = DR304.tcp(p["host"], p["port"], **kw, **sensor_kw)
        return sensor.connect()
    if str(p["baud"]).lower() == "auto":
        return _autodetect(p["port"], kw, sensor_kw)
    return DR304.serial(p["port"], int(p["baud"]), **kw, **sensor_kw).connect()


def _autodetect(port: str, kw: Dict[str, Any], sensor_kw: Dict[str, Any]) -> DR304:
    for baud in AUTO_BAUDS:
        sensor = DR304.serial(port, baud, **kw, **sensor_kw)
        try:
            return sensor.connect()
        except (ModbusError, OSError):
            sensor.close()
    raise ModbusError(f"no D.R304 answered on {port} at any of {AUTO_BAUDS} (unit {kw.get('unit', 1)})")


def open_stream(uri: Optional[str] = None, rate_hz: Optional[float] = None, *, tare: Optional[float] = None,
                buffer_seconds: float = 30.0, profile: Optional[str] = None, **connect_kw) -> FTStream:
    """Connect and start a background `FTStream` in one call.

        with dr304.open_stream(rate_hz=30, tare=1.0) as ft:     # URI from env / profile
            ft.start_recording("ep000_ft.csv")
            print(ft.latest().wrench)

    `tare=seconds` averages that long and subtracts it (keep the sensor unloaded).
    The sensor is closed when the stream is stopped.
    """
    uri, prof = resolve(uri, profile)
    if rate_hz is None:
        rate_hz = float(prof.get("rate_hz", DEFAULT_RATE_HZ))
    sensor = connect(uri, profile=profile, **connect_kw)
    stream = FTStream(sensor, rate_hz=rate_hz, buffer_seconds=buffer_seconds)
    try:
        stream.start()
        if tare:
            stream.tare(seconds=float(tare))
    except BaseException:
        stream.stop()
        raise
    return stream
