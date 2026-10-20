"""How to use the F/T sensor inside a data-collection / rollout loop.

The stream runs in its own thread at a fixed rate (here 120 Hz), independent of
your control loop.  Two ways to get the data into your dataset:

  A) per-step: call ft.latest() in your loop and store it with the observation;
  B) per-episode: ft.start_recording(...) writes every sample to CSV with
     time.time() and time.monotonic() stamps, so you can align it afterwards
     with camera / robot logs.

You can do both at once.

    python examples/rollout_logging.py --episodes 2                          # sensor from $DR304_URI / sensor.json
    python examples/rollout_logging.py --uri tcp://192.168.1.100 --rate 120
    dr304 sim &  python examples/rollout_logging.py --uri tcp://127.0.0.1:5020   # no hardware
"""
import argparse
import time

import dr304

ap = argparse.ArgumentParser()
ap.add_argument("--uri", help="tcp://HOST[:PORT] or serial:///dev/dr304 (default: env / profile)")
ap.add_argument("--rate", type=float, default=30)
ap.add_argument("--episodes", type=int, default=2)
ap.add_argument("--control-hz", type=float, default=30)
ap.add_argument("--out", default="ft_logs")
args = ap.parse_args()

ft = dr304.open_stream(args.uri, rate_hz=args.rate)
print("sensor:", ft.sensor, ft.sensor.config)

print("taring - keep the end effector still and unloaded")
bias = ft.tare(seconds=1.0)
print("bias:", bias)

for ep in range(args.episodes):
    ft.start_recording(f"{args.out}/episode_{ep:03d}_ft.csv", meta={"episode": ep, "task": "demo"})
    steps = []
    t_end = time.monotonic() + 3.0                      # pretend episode length
    while time.monotonic() < t_end:
        s = ft.latest()                                 # (A) newest sample, non-blocking
        steps.append({"t": s.t_mono, "ft": list(s.wrench)})
        # ... read cameras, command the robot ...
        time.sleep(1.0 / args.control_hz)
    info = ft.stop_recording()                          # (B) full-rate CSV + .json sidecar
    print(f"episode {ep}: {len(steps)} control steps, {info['samples']} F/T samples, "
          f"stats={ft.stats}")

    # high-rate F/T between two control steps, e.g. for contact detection:
    burst = ft.window(steps[-2]["t"], steps[-1]["t"])
    print(f"  {len(burst)} F/T samples between the last two control steps")

ft.stop()
