# dr304-ft

Python SDK and command-line tool for the **D.R304 six-axis force/torque transmitter** on Linux.
Read force/torque values, stream them at a fixed rate (30 / 60 / 120 Hz), and record them to CSV, over
**Ethernet (Modbus TCP)** or **USB-RS485 (Modbus RTU)**. No vendor software, no Windows, no GUI.

The vendor ships only a Windows GUI and publishes no protocol. This protocol was recovered from the vendor's own
programs and then checked against real hardware (see [docs/PROTOCOL_NOTES.md](docs/PROTOCOL_NOTES.md)).

```python
import dr304

with dr304.open_stream("tcp://192.168.1.100", rate_hz=30, tare=1.0) as ft:
    ft.start_recording("ep000_ft.csv", meta={"episode": 0})
    s = ft.latest()                   # call this every control step
    print(s.wrench)                   # Wrench(fx, fy, fz, mx, my, mz)   N and N.m
```

## What was measured on real hardware

One D.R304 unit, Ubuntu 22.04, Python 3.10 and 3.13:

| Link | Requested | Achieved | Errors | Notes |
|---|---|---|---|---|
| Ethernet (Modbus TCP) | 120 Hz | **120.0 Hz** | 0 | period 8.33 ± 0.30 ms, no gaps |
| USB-RS485 (Modbus RTU) | 30 Hz | **30.0 Hz** | 0 | period 33.3 ± 0.5 ms |
| USB-RS485 (Modbus RTU) | 60 Hz | 55.8 Hz | 0 | link limit: the transmitter needs ~11 ms to answer |

So: **use Ethernet for 60 or 120 Hz; USB-RS485 is fine for 30 Hz.** The stream warns when the link cannot keep up.

## Install

```bash
git clone <this repository> dr304-ft && cd dr304-ft
bash scripts/install.sh        # pip install -e ., udev rule (/dev/dr304), dialout group   (Ubuntu)
# library only:  pip install .      (dependency: pyserial)
```

Python 3.9+. No kernel driver is needed (Linux has `cp210x` built in).

## Connect

Tell the SDK where the sensor is once, then every call and the CLI work without arguments. A **URI**:

| URI | Meaning |
|---|---|
| `tcp://192.168.1.100` (or `:port`) | Ethernet, Modbus TCP, port 502 |
| `serial:///dev/dr304` | USB-RS485, Modbus RTU, 115200 baud |
| `serial:///dev/dr304?baud=auto` | try the common baud rates |
| `/dev/ttyUSB0`, `192.168.1.100` | bare device means serial, bare host means tcp |

If you pass no URI the SDK uses, in order: `$DR304_URI`, then a JSON profile found at `$DR304_PROFILE`,
`./sensor.json` or `~/.config/dr304/sensor.json`:

```json
{"uri": "serial:///dev/dr304", "rate_hz": 30, "unit": 1}
```

```bash
export DR304_URI=serial:///dev/dr304
dr304 check          # connect, read config, stream 3 s, report rate/errors: run this first
```

## Use

```python
import dr304

# 1. Direct reads, no background thread
with dr304.connect() as s:                    # URI from env / profile; or connect("tcp://...")
    w = s.read()                              # Wrench(fx, fy, fz, mx, my, mz)
    print(w.force, w.torque)

# 2. Background stream at a fixed rate (use this for data collection and rollouts)
with dr304.open_stream(rate_hz=30, tare=1.0) as ft:     # tare: average 1 s unloaded, subtract
    for ep in range(10):
        ft.start_recording(f"data/ep{ep:03d}_ft.csv", meta={"episode": ep})   # every sample to CSV
        while not done:
            s = ft.latest()                   # newest sample, non-blocking
            obs["ft"] = list(s.wrench)        # s.t_wall (time.time) and s.t_mono (time.monotonic) too
            ...                               # cameras, policy, robot command
        ft.stop_recording()                   # writes ep..._ft.csv and ep..._ft.csv.json
```

The stream polls in its own thread at a fixed rate, independent of your control loop, keeps the last 30 s in memory
(`ft.window(t0, t1)` returns the F/T samples between two timestamps, which is how you align with camera and robot
data), and recovers from short glitches by itself. Full reference: [docs/API.md](docs/API.md).
A complete rollout example: [examples/rollout_logging.py](examples/rollout_logging.py).

**Recorded files**: one CSV per recording, `seq,t_wall,t_mono,fx,fy,fz,mx,my,mz`, plus a `.json` sidecar with the
units, rate, tare offset, sensor settings and your `meta`. Units are whatever the transmitter is set to (N and N·m by default).

## Command line

```bash
dr304 check                                   # self test
dr304 ports                                   # list serial ports
dr304 info                                    # every parameter + one sample
dr304 read --tare                             # live values
dr304 record -r 30 -d 20 --tare -o ft.csv     # record 20 s at 30 Hz
dr304 bench                                   # maximum poll rate of this link
dr304 set filter_depth 16 --save              # write a parameter (asks first, -y to skip)
dr304 set-ip 192.168.1.100                    # write the network address and save
dr304 sim                                     # a fake sensor on 127.0.0.1:5020, to develop without hardware
```

Any command accepts `--uri ...`, `--tcp HOST[:PORT]` or `--serial DEV` to override the profile.

## Set up the hardware

Wiring, the transmitter's one-time configuration (Ethernet protocol, IP address, sample rate), network setup and
troubleshooting are in **[docs/DR304_FT_Sensor_Setup_Guide.md](docs/DR304_FT_Sensor_Setup_Guide.md)** (also as PDF).
In short: 24 V to the transmitter, RS485 A/B to a USB-RS485 adapter for the one-time configuration, then Ethernet
for logging. Our unit needed `eth_protocol 1`, `picking_rate 7` (800 Hz ADC) and `filter_depth 16`
(removes 50 Hz mains pickup at 800 Hz ADC), all set with `dr304 set ... --save`.

## Reliability notes

Measured behaviour of the transmitter that the SDK already handles (details in the protocol notes):

* The RS485 side ignores a request that comes less than ~2 ms after its previous reply, and then stays deaf until the
  line has been quiet for ~100 ms. The client enforces a 4 ms gap and recovers + retries once on a failure.
  `ft.stats["retries"]` counts retries. If you write your own raw Modbus client, you must do the same.
* Live force/torque are IEEE-754 **float32** values in the device's unit; they are not scaled integers.
* Use **one reader per sensor**. Small transmitters accept only a few simultaneous TCP clients.
* `tare()` is a software offset taken in the current pose. Tare with the same robot pose and gripper state each time;
  the tool's weight changes direction as the wrist rotates.

## Simulator and tests

```bash
dr304 sim &                                   # Modbus TCP on 127.0.0.1:5020
dr304 --tcp 127.0.0.1:5020 record -r 120 -d 3 -o sim.csv
pip install -e '.[test]' && pytest tests      # 34 tests, no hardware needed
```

## Status and limits

* Verified on one unit over Modbus TCP and Modbus RTU: configuration read/write, live data, recording, rates above.
* Not verified: `low_word_first` data order, the RS485 AutoSend mode and the vendor's DAS_TCP push mode
  (`dr304.das_tcp` is experimental and incomplete), other transmitter firmware versions.
* Data conditioning (filtering, gravity compensation) is deliberately not part of the SDK yet.
* This is an independent project, not affiliated with or supported by the manufacturer. Writing parameters changes
  the device's flash; `K1..K6` (factory correction factors) are read-only here on purpose.

## License

MIT, see [LICENSE](LICENSE).
