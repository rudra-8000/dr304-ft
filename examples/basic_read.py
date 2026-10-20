"""Read a few samples directly (no background thread).

    python examples/basic_read.py                              # sensor from $DR304_URI / sensor.json
    python examples/basic_read.py --uri tcp://192.168.1.100
    python examples/basic_read.py --uri serial:///dev/dr304
"""
import argparse
import time

import dr304

ap = argparse.ArgumentParser()
ap.add_argument("--uri", help="tcp://HOST[:PORT] or serial:///dev/dr304 (default: env / profile)")
args = ap.parse_args()

with dr304.connect(args.uri) as sensor:
    print(sensor, sensor.config)
    for _ in range(10):
        w = sensor.read()
        print(f"F = {w.force}  M = {w.torque}")
        time.sleep(0.1)
