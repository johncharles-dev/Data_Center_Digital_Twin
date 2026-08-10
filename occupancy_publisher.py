#!/usr/bin/env python3
"""Occupancy / workload telemetry source.

OccupancyTwin subscribes to datacenter/occupancy/staff and
datacenter/occupancy/workload, but nothing in this repository ever
published either topic — so load_factor was never set, the twin never
published state, and the orchestrator's load-vs-fault distinction was
fed a hardcoded default of 0 forever. This is that missing publisher.

Topics and payloads are exactly what twins/occupancy_twin.py reads:

  datacenter/occupancy/staff     {"timestamp": ..., "count": <int>}
  datacenter/occupancy/workload  {"timestamp": ...,
                                  "util_per_rack": {"SR-RACK-01": 0.0-1.0, ...}}

Runs on the same simulated clock as sensor_simulator.py (see its
"Simulated clock" docstring): one publish tick advances simulated time
by at most one training sample interval, so an accelerated demo changes
how fast you watch the day pass, not the shape of the day.

Usage:
  MQTT_HOST=localhost MQTT_PORT=1883 MQTT_TLS=false \
      python occupancy_publisher.py --interval 0.02
"""
import argparse
import json
import math
import os
import random
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from mqtt_identity import apply_credentials

RACK_IDS = ["SR-RACK-01", "SR-RACK-02", "SR-RACK-03"]

STAFF_TOPIC = "datacenter/occupancy/staff"
WORKLOAD_TOPIC = "datacenter/occupancy/workload"

SIM_SAMPLE_INTERVAL_S = 30.0  # must match sensor_simulator.py
PUBLISH_INTERVAL = 1.0

# Diurnal compute profile. Night floor 0.35, working-hours peak ~0.90 at
# about 13:00 — deliberately above orchestrator.LOAD_DRIVEN_FACTOR (0.8)
# so a busy room can actually explain elevated heat, and comfortably
# below it overnight so it cannot mask a genuine fault around the clock.
UTIL_FLOOR = 0.35
UTIL_PEAK_GAIN = 0.55
BUSY_START_H = 6.0
BUSY_LENGTH_H = 14.0

STAFF_MAX = 6


def _util_mean(hour_of_day):
    """Mean per-rack utilisation at a given hour, 0.0-1.0."""
    phase = (hour_of_day - BUSY_START_H) / BUSY_LENGTH_H
    if not 0.0 <= phase <= 1.0:
        return UTIL_FLOOR
    return UTIL_FLOOR + UTIL_PEAK_GAIN * math.sin(math.pi * phase)


def _staff_count(hour_of_day):
    """Headcount tracks the same working day, floored at 0 overnight."""
    phase = (hour_of_day - BUSY_START_H) / BUSY_LENGTH_H
    if not 0.0 <= phase <= 1.0:
        return 0
    return int(round(STAFF_MAX * math.sin(math.pi * phase)))


def main():
    ap = argparse.ArgumentParser(description="Occupancy/workload telemetry source")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--sim-step", type=float, default=SIM_SAMPLE_INTERVAL_S,
                    dest="sim_step",
                    help=f"simulated seconds per tick (clamped to {SIM_SAMPLE_INTERVAL_S})")
    ap.add_argument("--interval", type=float, default=PUBLISH_INTERVAL,
                    help="wall seconds between ticks")
    ap.add_argument("--start-hour", type=float, default=12.0, dest="start_hour",
                    help="simulated hour-of-day to start at (default midday, "
                         "i.e. busy — the state that exercises the "
                         "load-vs-fault branch)")
    args = ap.parse_args()

    sim_step = min(args.sim_step, SIM_SAMPLE_INTERVAL_S)

    use_tls = os.environ.get("MQTT_TLS", "false").lower() == "true"
    client = mqtt.Client(client_id="occupancy-publisher")
    if use_tls:
        client.tls_set()
    apply_credentials(client, "occupancy-publisher")
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    sim_epoch = time.time()
    sim_t = args.start_hour * 3600.0

    accel = sim_step / args.interval if args.interval else float("inf")
    print(f"Publishing {STAFF_TOPIC} + {WORKLOAD_TOPIC} to "
          f"{args.host}:{args.port}")
    print(f"Simulated clock: +{sim_step}s per tick -> {accel:.0f}x; "
          f"starting at {args.start_hour:.1f}h simulated\n")

    try:
        while True:
            hour = (sim_t / 3600.0) % 24
            stamp = datetime.fromtimestamp(
                sim_epoch + sim_t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            mean = _util_mean(hour)
            util = {rid: round(min(1.0, max(0.0, mean + random.uniform(-0.04, 0.04))), 3)
                    for rid in RACK_IDS}

            client.publish(STAFF_TOPIC, json.dumps(
                {"timestamp": stamp, "count": _staff_count(hour)}), qos=1)
            client.publish(WORKLOAD_TOPIC, json.dumps(
                {"timestamp": stamp, "util_per_rack": util}), qos=1)

            load_factor = round(sum(util.values()) / len(util), 3)
            print(f"h={hour:5.2f}  staff={_staff_count(hour)}  "
                  f"load_factor={load_factor:.3f}")

            time.sleep(args.interval)
            sim_t += sim_step
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        print("\nStopped.")


if __name__ == "__main__":
    main()
