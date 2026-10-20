"""Background acquisition at a fixed rate, with a ring buffer and CSV recording.

Typical rollout use::

    from dr304 import DR304, FTStream

    ft = FTStream(DR304.tcp("192.168.1.100"), rate_hz=120)
    ft.start()
    ft.tare()                                   # zero with the gripper unloaded
    for episode in range(n):
        ft.start_recording(f"data/ep{episode:03d}_ft.csv", meta={"episode": episode})
        while running:
            s = ft.latest()                     # newest Sample, never blocks
            obs["ft"] = s.wrench                # put it in your own dataset too
            ...
        ft.stop_recording()
    ft.stop()
"""

import collections
import csv
import json
import os
import queue
import threading
import time
import warnings
from typing import Callable, Deque, List, NamedTuple, Optional

from .modbus import ModbusError
from .sensor import DR304, Wrench

ZERO = Wrench(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

CSV_HEADER = ["seq", "t_wall", "t_mono", "fx", "fy", "fz", "mx", "my", "mz"]


class Sample(NamedTuple):
    seq: int            # monotonically increasing sample counter
    t_wall: float       # time.time() when the reply arrived (UNIX seconds)
    t_mono: float       # time.monotonic() at the same instant, for drift-free alignment
    wrench: Wrench      # tared values


class FTStream:
    """Poll a DR304 at `rate_hz` in a daemon thread.

    Thread-safe accessors: ``latest()``, ``window()``, ``since()``, ``wait_next()``.
    """

    def __init__(self, sensor: DR304, rate_hz: float = 120.0, buffer_seconds: float = 30.0,
                 max_consecutive_errors: int = 50, on_sample: Optional[Callable[[Sample], None]] = None):
        if rate_hz <= 0:
            raise ValueError("rate_hz must be > 0")
        self.sensor = sensor
        self.rate_hz = float(rate_hz)
        self.on_sample = on_sample
        self.max_consecutive_errors = max_consecutive_errors
        self._buf: Deque[Sample] = collections.deque(maxlen=max(1, int(buffer_seconds * rate_hz)))
        self._bias = ZERO
        self._lock = threading.Lock()
        self._new = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._seq = 0
        self._rec: Optional[_Recorder] = None
        self.stats = {"samples": 0, "errors": 0, "overruns": 0, "retries": 0, "last_error": ""}
        self._warned = False
        self.error: Optional[BaseException] = None

    # lifecycle -----------------------------------------------------------------
    def start(self) -> "FTStream":
        if self._thread and self._thread.is_alive():
            return self
        if not self.sensor.connected:
            self.sensor.connect()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dr304-stream", daemon=True)
        self._thread.start()
        # Block until the first sample so callers can use latest() right away.
        if self.wait_next(timeout=2.0) is None:
            raise TimeoutError(f"no data from sensor: {self.stats['last_error'] or self.error}")
        return self

    def stop(self) -> None:
        self.stop_recording()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.sensor.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # data access -------------------------------------------------------------------
    def latest(self) -> Optional[Sample]:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def wait_next(self, timeout: Optional[float] = None) -> Optional[Sample]:
        """Block until a new sample arrives; returns it (or None on timeout)."""
        with self._new:
            seq = self._seq
            self._new.wait_for(lambda: self._seq != seq or self._stop.is_set(), timeout)
            return self._buf[-1] if self._buf and self._seq != seq else None

    def window(self, t0: float, t1: Optional[float] = None, clock: str = "mono") -> List[Sample]:
        """Samples with t0 <= t <= t1 (clock 'mono' = time.monotonic, 'wall' = time.time)."""
        idx = 2 if clock == "mono" else 1
        t1 = float("inf") if t1 is None else t1
        with self._lock:
            return [s for s in self._buf if t0 <= s[idx] <= t1]

    def since(self, seq: int) -> List[Sample]:
        """All buffered samples newer than `seq` (use for lossless consumers)."""
        with self._lock:
            return [s for s in self._buf if s.seq > seq]

    def mean(self, seconds: float = 0.5) -> Wrench:
        now = time.monotonic()
        ss = self.window(now - seconds)
        if not ss:
            raise RuntimeError("no samples in window")
        return Wrench(*(sum(s.wrench[i] for s in ss) / len(ss) for i in range(6)))

    # tare ------------------------------------------------------------------------------
    def tare(self, seconds: float = 0.5) -> Wrench:
        """Software zero: average the next `seconds` of data and subtract it from now on."""
        t0 = time.monotonic()
        time.sleep(seconds)
        ss = self.window(t0)
        if not ss:
            raise RuntimeError("tare failed: no samples")
        with self._lock:
            mean_tared = Wrench(*(sum(s.wrench[i] for s in ss) / len(ss) for i in range(6)))
            self._bias = Wrench(*(b + m for b, m in zip(self._bias, mean_tared)))
        self.wait_next(timeout=1.0)  # so latest() already reflects the new zero
        return self._bias

    def clear_tare(self) -> None:
        with self._lock:
            self._bias = ZERO

    @property
    def bias(self) -> Wrench:
        return self._bias

    # recording -----------------------------------------------------------------------
    def start_recording(self, path: str, meta: Optional[dict] = None) -> str:
        """Append every sample to `path` (CSV) until stop_recording(). A `<path>.json`
        sidecar stores units, bias, rate and your `meta`."""
        self.stop_recording()
        c = self.sensor.config
        info = {
            "sensor": repr(self.sensor), "rate_hz": self.rate_hz,
            "force_unit": c.force_unit, "torque_unit": c.torque_unit,
            "force_decimals": c.force_decimals, "torque_decimals": c.torque_decimals,
            "device_picking_rate_hz": c.picking_rate_hz,
            "bias_subtracted": dict(zip(Wrench._fields, self._bias)),
            "started_wall": time.time(), "started_mono": time.monotonic(),
            "columns": CSV_HEADER, "meta": meta or {},
        }
        rec = _Recorder(path, info)
        with self._lock:
            self._rec = rec
        return path

    def stop_recording(self) -> Optional[dict]:
        with self._lock:
            rec, self._rec = self._rec, None
        return rec.close() if rec else None

    @property
    def recording(self) -> bool:
        return self._rec is not None

    # worker ---------------------------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.rate_hz
        next_t = time.monotonic()
        errors_in_row = 0
        while not self._stop.is_set():
            try:
                w = self.sensor.read()
                t_mono, t_wall = time.monotonic(), time.time()
                errors_in_row = 0
            except (ModbusError, OSError) as e:
                errors_in_row += 1
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)
                if errors_in_row >= self.max_consecutive_errors:
                    self.error = e
                    self._try_reconnect()
                    errors_in_row = 0
                w = None
            if w is not None:
                with self._new:
                    b = self._bias
                    s = Sample(self._seq + 1, t_wall, t_mono, Wrench(*(v - o for v, o in zip(w, b))))
                    self._seq = s.seq
                    self._buf.append(s)
                    rec = self._rec
                    self._new.notify_all()
                self.stats["samples"] += 1
                self.stats["retries"] = getattr(self.sensor.client, "retried", 0)
                if (self.stats["samples"] % 200 == 0 and not self._warned
                        and self.stats["overruns"] > 0.5 * self.stats["samples"]):
                    self._warned = True
                    warnings.warn(f"sensor link cannot sustain {self.rate_hz:g} Hz "
                                  f"(running behind on {self.stats['overruns']} of "
                                  f"{self.stats['samples']} samples); use Ethernet (Modbus TCP) "
                                  "or a lower rate", RuntimeWarning, stacklevel=2)
                if rec is not None:
                    rec.put(s)
                if self.on_sample is not None:
                    try:
                        self.on_sample(s)
                    except Exception:  # noqa: BLE001 - user callback must not kill acquisition
                        pass
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                self.stats["overruns"] += 1
                if delay < -5 * period:   # fell far behind; resync instead of bursting
                    next_t = time.monotonic()
        with self._new:
            self._new.notify_all()

    def _try_reconnect(self) -> None:
        try:
            self.sensor.close()
            time.sleep(0.5)
            self.sensor.connect()
        except Exception as e:  # noqa: BLE001
            self.stats["last_error"] = f"reconnect failed: {e}"
            time.sleep(1.0)


class _Recorder:
    """Writes samples to CSV from its own thread so disk I/O never stalls polling."""

    def __init__(self, path: str, info: dict):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self.info = info
        self.n = 0
        self._q: "queue.Queue[Optional[Sample]]" = queue.Queue()
        self._f = open(path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(CSV_HEADER)
        self._t = threading.Thread(target=self._loop, name="dr304-recorder", daemon=True)
        self._t.start()

    def put(self, s: Sample) -> None:
        self._q.put(s)

    def _loop(self) -> None:
        while True:
            s = self._q.get()
            if s is None:
                break
            self._w.writerow([s.seq, f"{s.t_wall:.6f}", f"{s.t_mono:.6f}", *(f"{v:.6g}" for v in s.wrench)])
            self.n += 1
            if self.n % 500 == 0:
                self._f.flush()

    def close(self) -> dict:
        self._q.put(None)
        self._t.join()
        self._f.close()
        self.info.update(samples=self.n, stopped_wall=time.time(), stopped_mono=time.monotonic())
        with open(self.path + ".json", "w") as f:
            json.dump(self.info, f, indent=2)
        return self.info
