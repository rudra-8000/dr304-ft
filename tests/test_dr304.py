import csv
import json
import time

import pytest

from dr304 import DR304, FTStream, Wrench
from dr304.das_tcp import FIELD_OFFSETS, FRAME_LEN, parse_frame
from dr304.modbus import ModbusError, ModbusRTUClient, crc16
from dr304.sensor import float32_to_regs, int32_to_regs, regs_to_float32, regs_to_int32
from dr304.sim import SimulatedDR304


def test_crc16_check_value():
    assert crc16(b"123456789") == 0x4B37  # CRC-16/MODBUS standard check value


@pytest.mark.parametrize("low", [False, True])
def test_int32_roundtrip(low):
    vals = [0, 1, -1, 123456, -987654, 2**31 - 1, -2**31]
    assert regs_to_int32(int32_to_regs(vals, low), low) == vals


@pytest.fixture(params=[False, True], ids=["high-first", "low-first"])
def sim(request):
    s = SimulatedDR304(low_word_first=request.param, force_decimals=2, torque_decimals=3)
    s.serve_tcp("127.0.0.1", 0)
    yield s
    s.stop()


def test_tcp_read_and_config(sim):
    with DR304.tcp("127.0.0.1", sim.tcp_port) as s:
        assert s.config.force_decimals == 2 and s.config.torque_decimals == 3
        assert s.config.force_unit == "N"
        w = s.read()
        assert isinstance(w, Wrench)
        assert -10.5 < w.fz < -9.0          # simulator: -9.81 +- 0.5
        assert abs(w.mx) <= 0.11
        assert s.get_ip() == ("192.168.1.100", 502)


def test_set_ip_and_param(sim):
    with DR304.tcp("127.0.0.1", sim.tcp_port) as s:
        s.set_ip("192.168.100.150", 502, save=True)
        assert s.get_ip() == ("192.168.100.150", 502)
        s.set_param("packet_interval", 0.005)
        assert s.get_param("packet_interval") == pytest.approx(0.005)
        with pytest.raises(ValueError):
            s.set_param("k1_fx", 2)


def test_rtu_over_pty():
    sim = SimulatedDR304()
    path = sim.serve_pty()
    try:
        with DR304.serial(path, 115200, timeout=0.5) as s:
            assert -10.5 < s.read().fz < -9.0
            assert s.get_param("picking_rate") == 7
    finally:
        sim.stop()


@pytest.mark.parametrize("rate", [30, 60, 120])
def test_stream_rate_and_recording(sim, tmp_path, rate):
    out = tmp_path / "ep.csv"
    with FTStream(DR304.tcp("127.0.0.1", sim.tcp_port), rate_hz=rate) as ft:
        ft.tare(0.2)
        assert abs(ft.latest().wrench.fz) < 1.0  # tared
        ft.start_recording(str(out), meta={"episode": 3})
        time.sleep(1.0)
        info = ft.stop_recording()
    rows = list(csv.DictReader(open(out)))
    assert len(rows) == info["samples"]
    assert 0.85 * rate <= len(rows) <= 1.1 * rate
    seqs = [int(r["seq"]) for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    meta = json.load(open(str(out) + ".json"))
    assert meta["meta"]["episode"] == 3 and meta["rate_hz"] == rate
    assert meta["bias_subtracted"]["fz"] < -9


def test_window_and_wait(sim):
    with FTStream(DR304.tcp("127.0.0.1", sim.tcp_port), rate_hz=100) as ft:
        t0 = time.monotonic()
        time.sleep(0.3)
        w = ft.window(t0)
        assert 20 <= len(w) <= 40
        s = ft.wait_next(timeout=0.5)
        assert s is not None and s.seq > w[-1].seq
        assert all(x.seq > w[-1].seq for x in ft.since(w[-1].seq))


def test_das_frame_parse():
    frame = bytearray(b" " * FRAME_LEN)
    for i, off in enumerate(FIELD_OFFSETS):
        frame[off:off + 7] = f"{(i - 3) * 1234:+07d}".encode()
    assert parse_frame(bytes(frame)) == [(i - 3) * 1234 for i in range(6)]
    frame[FIELD_OFFSETS[2]] = ord("x")
    assert parse_frame(bytes(frame)) is None


@pytest.mark.parametrize("low", [False, True])
def test_float32_roundtrip(low):
    vals = [78.28, -28.56, 32.96, -0.987, -1.62, 3.04]
    out = regs_to_float32(float32_to_regs(vals, low), low)
    assert out == pytest.approx(vals, rel=1e-6)
    # real capture from the transmitter (high word first): 0x429c 0x8f5c
    assert regs_to_float32([0x429C, 0x8F5C])[0] == pytest.approx(78.28, abs=1e-3)


class _FakeSerial:
    """Stands in for pyserial: replies to each write from a script; None = stay silent."""

    def __init__(self, script):
        self.script = list(script)
        self.writes = []
        self.rx = b""

    def reset_input_buffer(self):
        self.rx = b""

    def write(self, data):
        self.writes.append((time.monotonic(), bytes(data)))
        reply = self.script.pop(0)
        self.rx = b"" if reply is None else reply

    def read(self, n):
        out, self.rx = self.rx[:n], self.rx[n:]
        return out

    def close(self):
        pass


def _reply(unit, regs):
    body = bytes([unit, 3, 2 * len(regs)]) + b"".join(r.to_bytes(2, "big") for r in regs)
    return body + crc16(body).to_bytes(2, "little")


def test_rtu_enforces_gap_and_recovers_from_deaf_period():
    good = _reply(1, [0x429C, 0x8F5C])
    c = ModbusRTUClient("fake", min_gap=0.02, recovery=0.05, retries=1)
    c._ser = _FakeSerial([good, None, good])
    assert c.read_holding(0x400, 2) == [0x429C, 0x8F5C]
    assert c.read_holding(0x400, 2) == [0x429C, 0x8F5C]     # first try silent -> retried
    assert c.retried == 1
    t = [w[0] for w in c._ser.writes]
    assert t[1] - t[0] >= 0.02          # min_gap after a reply
    assert t[2] - t[1] >= 0.05          # recovery silence after a timeout


def test_rtu_gives_up_after_retries():
    c = ModbusRTUClient("fake", min_gap=0.0, recovery=0.0, retries=1)
    c._ser = _FakeSerial([None, None])
    with pytest.raises(ModbusError):
        c.read_holding(0x400, 2)


# --- URIs, profiles, one-call helpers --------------------------------------------------
import json

import dr304
from dr304.profile import ConfigError, parse_uri, resolve


@pytest.mark.parametrize("uri,expect", [
    ("tcp://192.168.1.100", ("tcp", "192.168.1.100", 502)),
    ("tcp://10.0.0.5:1502", ("tcp", "10.0.0.5", 1502)),
    ("192.168.1.100", ("tcp", "192.168.1.100", 502)),
    ("serial:///dev/dr304", ("serial", "/dev/dr304", "115200")),
    ("serial:///dev/dr304?baud=auto", ("serial", "/dev/dr304", "auto")),
    ("/dev/ttyUSB0", ("serial", "/dev/ttyUSB0", "115200")),
])
def test_parse_uri(uri, expect):
    p = parse_uri(uri)
    if p["kind"] == "tcp":
        got = ("tcp", p["host"], p["port"])
    else:
        got = ("serial", p["port"], p["baud"])
    assert got == expect


@pytest.mark.parametrize("bad", ["", "http://x", "tcp://", "serial://"])
def test_parse_uri_rejects(bad):
    with pytest.raises(ConfigError):
        parse_uri(bad)


def test_resolve_precedence(tmp_path, monkeypatch):
    prof = tmp_path / "sensor.json"
    prof.write_text(json.dumps({"uri": "tcp://1.1.1.1", "rate_hz": 60, "unit": 3}))
    monkeypatch.delenv("DR304_URI", raising=False)
    monkeypatch.setenv("DR304_PROFILE", str(prof))
    assert resolve()[0] == "tcp://1.1.1.1"                       # profile
    monkeypatch.setenv("DR304_URI", "tcp://2.2.2.2")
    assert resolve()[0] == "tcp://2.2.2.2"                       # env beats profile
    assert resolve("tcp://3.3.3.3")[0] == "tcp://3.3.3.3"        # argument beats env


def test_resolve_without_anything_explains(tmp_path, monkeypatch):
    monkeypatch.delenv("DR304_URI", raising=False)
    monkeypatch.delenv("DR304_PROFILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="DR304_URI"):
        resolve()


def test_connect_and_open_stream_via_uri_and_profile(tmp_path, monkeypatch):
    sim = SimulatedDR304()
    sim.serve_tcp("127.0.0.1", 0)
    uri = f"tcp://127.0.0.1:{sim.tcp_port}"
    try:
        with dr304.connect(uri) as s:
            assert isinstance(s.read(), Wrench)
        prof = tmp_path / "p.json"
        prof.write_text(json.dumps({"uri": uri, "rate_hz": 60}))
        monkeypatch.delenv("DR304_URI", raising=False)
        with dr304.open_stream(profile=str(prof), tare=0.2) as ft:   # rate from the profile
            assert ft.rate_hz == 60
            time.sleep(0.3)
            assert ft.latest() is not None
            assert any(abs(b) > 0 for b in ft.bias)       # tare captured a bias
        assert not ft.running
    finally:
        sim.stop()
