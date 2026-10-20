# Changelog

## 0.2.0 - 2026-10-20

First hardware-verified release.

* `dr304.connect()` / `dr304.open_stream()`: one-call setup from a URI, `$DR304_URI` or a `sensor.json` profile.
* `dr304 check`: end-to-end self test. CLI commands now fall back to the URI/profile when no `--tcp/--serial` is given.
* **Fix:** live force/torque are float32 in physical units (previously decoded as scaled int32).
* **Fix:** USB-RS485 client enforces the transmitter's inter-request gap (4 ms) and recovers from its "deaf period";
  before, every second request was lost and `--baud auto` reported "no device".
* Stream warns when the link cannot sustain the requested rate; `stats["retries"]`.
* Measured on one unit: 120.0 Hz over Ethernet with 0 errors; 30 Hz exact and ~55 Hz maximum over USB-RS485.
* Requires Python 3.9+.

## 0.1.0

Initial driver, streamer, CSV recorder, simulator and CLI (simulator-tested only).
