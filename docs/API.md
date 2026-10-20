# dr304 API reference

```python
import dr304
dr304.__version__      # "0.2.0"
```

All classes are thread-safe unless noted. Units are those configured on the transmitter, N and N·m by default
(`sensor.config.force_unit` / `.torque_unit`).

## Connecting

### `dr304.connect(uri=None, *, unit=None, timeout=None, profile=None) -> DR304`

Open and return a connected `DR304`. Also a context manager. The URI is resolved from the argument, then `$DR304_URI`,
then the profile file. Raises `ConfigError` (no/invalid URI), `ModbusError` (no answer) or `OSError` (port/socket).

```python
with dr304.connect("tcp://192.168.1.100") as s: ...
s = dr304.connect("serial:///dev/dr304?baud=auto")      # tries 115200, 19200, 9600, ...
```

### `dr304.open_stream(uri=None, rate_hz=None, *, tare=None, buffer_seconds=30.0, profile=None, **connect_kw) -> FTStream`

Connect and start a background stream. `rate_hz` defaults to the profile's `rate_hz`, else 30. `tare=1.0` averages 1 s
and subtracts it (keep the sensor unloaded). Stopping the stream (or leaving the `with` block) closes the sensor.

### URIs and the profile file

| URI | Meaning |
|---|---|
| `tcp://HOST[:PORT]` | Modbus TCP (default port 502) |
| `serial:///dev/dr304[?baud=115200\|auto][&unit=1][&timeout=0.1]` | Modbus RTU over USB-RS485 |
| `/dev/...` / `HOST` | bare forms of the above |

Profile (JSON), looked up at `$DR304_PROFILE`, `./sensor.json`, `~/.config/dr304/sensor.json` (first that exists):

| Key | Meaning |
|---|---|
| `uri` | sensor URI (required) |
| `rate_hz` | default stream rate for `open_stream` (default 30) |
| `unit` | Modbus slave id (default 1) |
| `timeout` | per-request timeout in seconds (defaults: serial 0.1, tcp 0.2) |

Other keys are ignored, so a profile may also carry notes for people and agents. `dr304.load_profile(path=None)` returns it
as a dict (`{}` if none); `dr304.parse_uri(uri)` splits a URI.

## `DR304`: one sensor, direct access

Created by `connect()`, or manually with `DR304.tcp(host, port=502, unit=1, timeout=0.2)` /
`DR304.serial(port, baudrate=115200, unit=1, timeout=0.1)`; then `.connect()` (or `with`). Calls from several threads are
serialised, but use **one reader per sensor**.

| Member | Description |
|---|---|
| `read() -> Wrench` | one sample, in device units (blocks ~13 ms on serial, ~1 ms on Ethernet) |
| `read_raw() -> list[float]` | the six values as sent by the device |
| `read_mvv_raw() -> list[int]` | raw bridge outputs (mV/V counts), diagnostics |
| `config -> SensorConfig` | `force_unit`, `torque_unit`, `force_decimals`, `torque_decimals`, `picking_rate_hz`, `low_word_first`, `live_format` |
| `refresh_config()` | re-read the above from the device |
| `get_param(name)`, `get_params()` | read parameters (names in `dr304.registers.PARAMS`) |
| `set_param(name, value, save=False)` | write one parameter. **`save=True` writes flash.** Without it the value reverts at power cycle |
| `save()` | persist parameters to flash (power-cycle the transmitter afterwards for network settings) |
| `get_ip()`, `set_ip(ip, port=502, save=True)` | network address (applies after a power cycle) |
| `close()`, `connected` | |

## `FTStream`: background fixed-rate stream

`FTStream(sensor, rate_hz=120.0, buffer_seconds=30.0, max_consecutive_errors=50, on_sample=None)`; usually obtained from
`open_stream()`. Polls in a daemon thread with a deadline schedule (no drift) and keeps a ring buffer.

| Member | Description |
|---|---|
| `start()` | connect if needed, start, **block until the first sample**; returns `self`. Raises `TimeoutError` after 2 s |
| `stop()` | stop, finish any recording, close the sensor. Also `with` |
| `latest() -> Sample \| None` | newest sample, non-blocking |
| `wait_next(timeout=None) -> Sample \| None` | block until a new sample |
| `window(t0, t1=None, clock="mono") -> list[Sample]` | buffered samples with `t0 <= t <= t1`; `clock` is `"mono"` (`time.monotonic`) or `"wall"` (`time.time`) |
| `since(seq) -> list[Sample]` | all buffered samples newer than `seq` (lossless consumers slower than the stream) |
| `mean(seconds=0.5) -> Wrench` | average of the last `seconds` |
| `tare(seconds=0.5) -> Wrench` | software zero: average the next `seconds` of data and subtract it from now on; returns the total bias. The sensor's own zero is not touched |
| `clear_tare()`, `bias` | remove / inspect the software offset |
| `start_recording(path, meta=None)` | write every sample to `path` (CSV) from a separate writer thread, plus `path + ".json"` on stop |
| `stop_recording() -> dict \| None` | close the file; returns the sidecar info (`samples`, start/stop times, ...) |
| `recording`, `running`, `rate_hz` | state |
| `stats` | dict: `samples`, `errors`, `overruns`, `retries`, `last_error` |
| `error` | set when the stream gave up and had to reconnect |

`overruns` counts cycles that finished later than the period. If it is close to `samples`, the link cannot sustain
`rate_hz` (a `RuntimeWarning` is also issued); lower the rate or use Ethernet. After `max_consecutive_errors` failed reads
in a row the stream closes and reopens the connection by itself.

`on_sample(sample)` is called from the polling thread for every sample; keep it fast. Exceptions in it are swallowed.

### `Sample`

`NamedTuple(seq, t_wall, t_mono, wrench)`: `seq` counts samples from 1; `t_wall` = `time.time()` and `t_mono` =
`time.monotonic()` when the reply arrived (the measurement is ~10 ms older on serial and ~1 ms on Ethernet);
`wrench` is already tared.

### `Wrench`

`NamedTuple(fx, fy, fz, mx, my, mz)` with `.force`, `.torque` tuples and `a - b` subtraction.

## CSV and sidecar format

`start_recording("ft.csv")` writes:

```
seq,t_wall,t_mono,fx,fy,fz,mx,my,mz
2,1792486356.778068,2686571.764904,78.23,-26.56,32.63,-0.942,-1.622,3.039
```

Values are tared. `ft.csv.json` holds `sensor`, `rate_hz`, `force_unit`, `torque_unit`, the decimals,
`device_picking_rate_hz`, `bias_subtracted` (so raw = value + bias), `started_wall/mono`, `stopped_wall/mono`,
`samples`, `columns` and your `meta`. To align with other logs use `t_mono` within one process, `t_wall` across machines
(whose clocks you must have synchronised).

## Exceptions

| Exception | Meaning |
|---|---|
| `dr304.ConfigError` (`ValueError`) | missing or malformed URI/profile |
| `dr304.ModbusError` (`IOError`) | timeout, CRC error, wrong unit, device refused |
| `dr304.modbus.ModbusException` | the device returned a Modbus exception response (subclass of `ModbusError`) |

## Lower level

`dr304.modbus.ModbusRTUClient / ModbusTCPClient` (`read_holding`, `write_registers`), `dr304.registers` (addresses, parameter
table, enums), `dr304.sim.SimulatedDR304` (fake sensor for tests: `serve_tcp(host, port)`, `serve_pty()`, `stop()`),
`dr304.das_tcp` (experimental vendor push protocol).

## CLI

`dr304 --help`, or the README. Exit codes: `0` ok, `1` failure, `2` `check` finished with warnings.
