"""`dr304` command line tool.

    dr304 check                                  # test everything using the sensor from $DR304_URI / sensor.json
    dr304 ports                                  # find the USB-RS485 adapter
    dr304 --serial /dev/dr304 info               # read every parameter
    dr304 --tcp 192.168.1.100 read             # live values
    dr304 --tcp 192.168.1.100 record -r 120 -o ft.csv -d 10 --tare
    dr304 --tcp 192.168.1.100 bench            # achievable poll rate
    dr304 --serial /dev/dr304 set-ip 192.168.1.100
    dr304 --serial /dev/dr304 set picking_rate 7 --save
    dr304 sim                                    # fake sensor on 127.0.0.1:5020
"""

import argparse
import sys
import time

from . import registers as R
from .modbus import ModbusError
from .profile import AUTO_BAUDS, ConfigError, connect, resolve
from .sensor import DR304
from .stream import FTStream


def _sensor(args) -> DR304:
    unit = 1 if args.unit is None else args.unit
    if args.tcp:
        host, _, port = args.tcp.partition(":")
        return DR304.tcp(host, int(port or 502), unit=unit, timeout=args.timeout)
    if args.serial:
        if args.baud == "auto":
            for b in AUTO_BAUDS:
                s = DR304.serial(args.serial, b, unit=unit, timeout=args.timeout)
                try:
                    s.connect()
                    print(f"# found device at {b} baud", file=sys.stderr)
                    return s
                except (ModbusError, OSError):
                    s.close()
            sys.exit(f"no D.R304 answered on {args.serial} at any baud rate (unit {unit})")
        return DR304.serial(args.serial, int(args.baud), unit=unit, timeout=args.timeout)
    try:   # no --tcp/--serial: use --uri, $DR304_URI or the profile file
        return connect(args.uri, profile=args.profile, unit=args.unit)
    except ConfigError as e:
        sys.exit(f"{e}\n(or use --tcp HOST[:PORT] / --serial /dev/dr304)")
    except (ModbusError, OSError) as e:
        sys.exit(f"cannot talk to the sensor: {e}" + _hint_line(e))


def _open(args) -> DR304:
    s = _sensor(args)
    if not s.connected:
        try:
            s.connect()
        except (ModbusError, OSError) as e:
            sys.exit(f"cannot talk to {s}: {e}" + _hint_line(e))
    return s


def cmd_ports(args):
    from serial.tools import list_ports
    found = False
    for p in list_ports.comports():
        tag = "  <-- CP210x (UT-891 USB-RS485?)" if (p.vid, p.pid) == (0x10C4, 0xEA60) else ""
        print(f"{p.device:16s} {p.vid and f'{p.vid:04x}:{p.pid:04x}' or '-':10s} {p.description}{tag}")
        found = True
    if not found:
        print("no serial ports found")


def cmd_info(args):
    s = _open(args)
    c = s.config
    print(f"device       : {s}")
    print(f"word order   : {'low' if c.low_word_first else 'high'} word first")
    print(f"units        : {c.force_unit} / {c.torque_unit}   decimals {c.force_decimals}/{c.torque_decimals}")
    try:
        ip, port = s.get_ip()
        print(f"ethernet     : {ip}:{port}")
    except ModbusError as e:
        print(f"ethernet     : ? ({e})")
    print()
    for name, val in s.get_params().items():
        p = R.PARAMS[name]
        d = R.describe(name, val) if val == val else None
        print(f"  {name:22s} [{p.address}] = {val:<10g} {('(' + d + ')') if d else ''}")
    print()
    print("raw values   :", s.read_raw())
    print("wrench       :", _fmt(s.read()))
    print("mV/V (raw)   :", s.read_mvv_raw())
    s.close()


def _fmt(w):
    return "  ".join(f"{n}={v:+9.3f}" for n, v in zip(w._fields, w))


def cmd_read(args):
    s = _open(args)
    with FTStream(s, rate_hz=args.rate) as ft:
        if args.tare:
            print("taring...", file=sys.stderr)
            ft.tare()
        try:
            while True:
                smp = ft.latest()
                print(f"\r#{smp.seq:7d} {_fmt(smp.wrench)}", end="", flush=True)
                time.sleep(1.0 / min(args.rate, 20))
        except KeyboardInterrupt:
            print()


def cmd_record(args):
    s = _open(args)
    with FTStream(s, rate_hz=args.rate) as ft:
        if args.tare:
            print("taring (keep the sensor unloaded)...", file=sys.stderr)
            ft.tare()
        ft.start_recording(args.out, meta={"note": args.note} if args.note else None)
        print(f"recording {args.rate:g} Hz -> {args.out}  (Ctrl-C to stop)", file=sys.stderr)
        t0 = time.monotonic()
        try:
            while args.duration is None or time.monotonic() - t0 < args.duration:
                time.sleep(0.5)
                print(f"\r{time.monotonic() - t0:7.1f}s  samples={ft.stats['samples']} "
                      f"errors={ft.stats['errors']} overruns={ft.stats['overruns']}", end="", file=sys.stderr)
        except KeyboardInterrupt:
            pass
        info = ft.stop_recording()
        dt = info["stopped_mono"] - info["started_mono"]
        print(f"\nwrote {info['samples']} samples in {dt:.1f}s ({info['samples'] / max(dt, 1e-9):.1f} Hz)",
              file=sys.stderr)


def cmd_check(args):
    """End-to-end self test: connect, read the configuration, stream for a few seconds, report."""
    def step(name, fn):
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 - report any failure and stop
            print(f"[FAIL] {name}: {e}")
            hint = _hint(e)
            if hint:
                print(f"       -> {hint}")
            sys.exit(1)
        print(f"[ OK ] {name}" + (f": {out}" if out else ""))
        return out

    box = {}
    step("sensor found", lambda: box.setdefault("s", _open(args)) and repr(box["s"]))
    s = box["s"]
    c = s.config
    step("configuration", lambda: f"{c.force_unit}/{c.torque_unit}, ADC {c.picking_rate_hz} Hz, "
                                  f"{'low' if c.low_word_first else 'high'}-word-first, {c.live_format}")
    w = step("single read", lambda: _fmt(s.read()))
    rate = args.rate
    ft = FTStream(s, rate_hz=rate)
    step(f"stream {args.seconds:g} s at {rate:g} Hz", lambda: (ft.start(), time.sleep(args.seconds)) and None)
    n = ft.stats["samples"]
    got = n / args.seconds
    st = dict(ft.stats)
    ft.stop()
    ok_rate = got >= 0.95 * rate
    print(f"[{' OK ' if ok_rate else 'WARN'}] achieved {got:.1f} Hz of {rate:g} Hz "
          f"(errors={st['errors']} overruns={st['overruns']} retries={st['retries']})")
    if not ok_rate:
        print("       -> the link cannot sustain this rate; USB-RS485 tops out near 55 Hz, use Ethernet above that")
    if c.picking_rate_hz and c.picking_rate_hz < rate:
        print(f"[WARN] sensor ADC rate ({c.picking_rate_hz} Hz) is below {rate:g} Hz: samples repeat. "
              "Set picking_rate 7 (800 Hz)")
    print("all checks passed" if ok_rate and st["errors"] == 0 else "finished with warnings")
    sys.exit(0 if ok_rate and st["errors"] == 0 else 2)


def _hint_line(e: Exception) -> str:
    h = _hint(e)
    return f"\n  -> {h}" if h else ""


def _hint(e: Exception) -> str:
    msg = str(e).lower()
    if isinstance(e, PermissionError) or "permission denied" in msg:
        return "no permission on the serial port: sudo usermod -aG dialout $USER, then log out and in"
    if "no such file" in msg or "could not open port" in msg:
        return "device not found: run `dr304 ports`; is the udev rule installed (scripts/install.sh)?"
    if "timed out" in msg or "timeout" in msg:
        return ("no answer. serial: is the transmitter powered (24 V), A/B wiring, rs485_protocol = 1, "
                "adapter plugged in (`dr304 ports`)? tcp: `ping` it, check which NIC/subnet it is on, eth_protocol = 1")
    return ""


def cmd_bench(args):
    s = _open(args)
    n, t0 = 0, time.perf_counter()
    lat = []
    while time.perf_counter() - t0 < args.seconds:
        a = time.perf_counter()
        s.read_raw()
        lat.append(time.perf_counter() - a)
        n += 1
    dt = time.perf_counter() - t0
    lat.sort()
    print(f"{n} reads in {dt:.2f}s = {n / dt:.0f} Hz max; latency median {1e3 * lat[len(lat) // 2]:.2f} ms, "
          f"p99 {1e3 * lat[int(len(lat) * 0.99)]:.2f} ms")
    s.close()


def _confirm(args, msg):
    if args.yes:
        return
    if input(f"{msg} [y/N] ").strip().lower() != "y":
        sys.exit("aborted")


def cmd_set_ip(args):
    s = _open(args)
    old = s.get_ip()
    print(f"current: {old[0]}:{old[1]}")
    _confirm(args, f"write {args.ip}:{args.port} and save to flash?")
    s.set_ip(args.ip, args.port, save=True)
    print("new    : %s:%d" % s.get_ip())
    print("Now power the transmitter off for 10 s and back on.")


def cmd_set(args):
    s = _open(args)
    if args.name not in R.PARAMS:
        sys.exit(f"unknown parameter; choose from: {', '.join(R.PARAMS)}")
    old = s.get_param(args.name)
    _confirm(args, f"{args.name}: {old:g} -> {args.value:g}{' and save' if args.save else ''}?")
    s.set_param(args.name, args.value, save=args.save)
    print(f"{args.name} = {s.get_param(args.name):g}")
    if args.save:
        print("Saved. Power-cycle the transmitter (10 s off) for it to take effect.")


def cmd_sim(args):
    from .sim import SimulatedDR304
    sim = SimulatedDR304()
    sim.serve_tcp(args.host, args.port)
    print(f"simulated D.R304 on {args.host}:{sim.tcp_port}  (try: dr304 --tcp {args.host}:{sim.tcp_port} read)", flush=True)
    if args.pty:
        print(f"Modbus RTU on {sim.serve_pty()}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


def cmd_das_dump(args):
    from .das_tcp import dump
    if not args.tcp:
        sys.exit("--tcp required")
    host, _, port = args.tcp.partition(":")
    n = dump(host, int(port or 502), args.seconds, args.out)
    print(f"wrote {n} bytes to {args.out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dr304", description="D.R304 six-axis F/T sensor tool",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--uri", help="tcp://HOST[:PORT] or serial:///dev/dr304 (default: $DR304_URI or profile file)")
    ap.add_argument("--profile", metavar="FILE", help="sensor.json profile (default: $DR304_PROFILE, ./sensor.json, "
                    "~/.config/dr304/sensor.json)")
    ap.add_argument("--tcp", metavar="HOST[:PORT]", help="Ethernet (Modbus TCP, Ethernet protocol = 1)")
    ap.add_argument("--serial", metavar="DEV", help="USB-RS485 device (Modbus RTU), e.g. /dev/dr304")
    ap.add_argument("--baud", default="115200", help="RS485 baud rate or 'auto' (default 115200)")
    ap.add_argument("--unit", type=int, default=None, help="Modbus slave id / machine code (default 1)")
    ap.add_argument("--timeout", type=float, default=0.2, help="per-request timeout, s")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports").set_defaults(fn=cmd_ports)
    sub.add_parser("info", help="print all parameters and one sample").set_defaults(fn=cmd_info)

    p = sub.add_parser("read", help="print live values")
    p.add_argument("-r", "--rate", type=float, default=60)
    p.add_argument("--tare", action="store_true")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("record", help="record to CSV")
    p.add_argument("-r", "--rate", type=float, default=120, help="Hz (e.g. 30, 60, 120)")
    p.add_argument("-o", "--out", default=time.strftime("ft_%Y%m%d_%H%M%S.csv"))
    p.add_argument("-d", "--duration", type=float, help="seconds (default: until Ctrl-C)")
    p.add_argument("--tare", action="store_true", help="software zero before recording")
    p.add_argument("--note", help="free text stored in the .json sidecar")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("check", help="self test: connect, read config, stream, report")
    p.add_argument("-r", "--rate", type=float, default=30)
    p.add_argument("-s", "--seconds", type=float, default=3)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("bench", help="measure max poll rate")
    p.add_argument("-s", "--seconds", type=float, default=3)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("set-ip", help="write the transmitter's IP/port and save")
    p.add_argument("ip")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(fn=cmd_set_ip)

    p = sub.add_parser("set", help="write one parameter (see `info` for names)")
    p.add_argument("name")
    p.add_argument("value", type=float)
    p.add_argument("--save", action="store_true", help="persist to flash")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("sim", help="run a simulated sensor for testing")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5020)
    p.add_argument("--pty", action="store_true", help="also serve Modbus RTU on a pseudo-terminal")
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("das-dump", help="(experimental) save raw DAS_TCP stream (Ethernet protocol = 2)")
    p.add_argument("-s", "--seconds", type=float, default=3)
    p.add_argument("-o", "--out", default="das_dump.bin")
    p.set_defaults(fn=cmd_das_dump)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
