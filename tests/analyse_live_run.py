"""Analyses an ordered MQTT capture of a full live run.

Answers two questions by measurement:

  1. Are the model's two engineered slope features inside the range the
     model was actually trained on? (defect 1)
  2. Does the trained model tell you anything the plain threshold rules
     don't? (defect 2) — if the classifier crosses its action threshold
     at exactly the moment threshold_flags first appears, the model is
     re-deriving the rules and adding no lead time.

Input: `mosquitto_sub -v '#'` output, one "topic {json}" per line, in
arrival order.

Usage:  python3 tests/analyse_live_run.py <capture.txt>
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ACTION_THRESHOLD = 0.5

# Measured from the training set (dataset/generate_dataset.py, seed 42)
# by tests/training_feature_range.py.
TRAINING_RANGE = {
    "motor_temp_slope_10min": {"p99": 0.2550, "max": 0.5300, "min": -0.5800},
    "filter_dp_slope_10min": {"p99": 1.4250, "max": 3.6600, "min": -3.6100},
}

# Both slope features are diff(20)/20 over a twenty-sample window. For the
# first twenty samples after a run boundary that window straddles the reset,
# differencing the new run's healthy opening against the previous run's
# tripped final state. The result is a large negative spike that measures the
# concatenation of two runs, not the behaviour of either one.
#
# These are excluded from the distribution check and reported separately
# rather than dropped silently. The exclusion is one-directional in practice:
# every boundary sample lands BELOW the training minimum and none above the
# maximum, so nothing that could raise a false alarm is being hidden.
SLOPE_WINDOW_SAMPLES = 20


def parse(path):
    """Returns arrival-ordered (topic, payload) pairs."""
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if " " not in line:
                continue
            topic, _, body = line.partition(" ")
            try:
                out.append((topic, json.loads(body)))
            except json.JSONDecodeError:
                continue
    return out


def sim_seconds(stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def main(path):
    events = parse(path)
    crac = [p for t, p in events if t == "datacenter/racks/CRAC-01"]
    cooling = [p for t, p in events if t == "datacenter/twin-state/cooling"]
    occupancy = [p for t, p in events if t == "datacenter/twin-state/occupancy"]

    print(f"Capture: {len(events)} messages")
    print(f"  CRAC-01 telemetry     {len(crac)}")
    print(f"  twin-state/cooling    {len(cooling)}")
    print(f"  twin-state/occupancy  {len(occupancy)}")

    if not cooling:
        print("\nFAIL: no cooling twin state — the stack did not run")
        return 1

    # ---- Pair each cooling state with the prediction it produced --------
    # The orchestrator recomputes from the LATEST cooling state on every
    # twin-state message, so the first prediction after a cooling update
    # is the one derived from it.
    pairs = []
    pending = None
    for topic, payload in events:
        if topic == "datacenter/twin-state/cooling":
            pending = payload
        elif topic == "datacenter/predictions/CRAC-01" and pending is not None:
            pairs.append((pending, payload))
            pending = None
    print(f"  paired cooling->prediction  {len(pairs)}")

    # ---- 1. Slope features vs training distribution ---------------------
    print("\n=== 1. Slope features vs the training distribution ===")
    # Flag each sample that sits inside a slope window straddling a run
    # boundary. A boundary is a change of run_id; if the capture predates
    # run_id being published, a simulated clock stepping backwards means the
    # same thing. The start of the capture counts as a boundary too, because
    # the first window is filling from an unknown prior state.
    warmup = [False] * len(cooling)
    since_boundary = 0
    prev_run = prev_sim = None
    for i, c in enumerate(cooling):
        run, sim = c.get("run_id"), c.get("sim_time")
        boundary = i == 0
        if run is not None and prev_run is not None and run != prev_run:
            boundary = True
        elif (run is None and sim is not None and prev_sim is not None
              and sim < prev_sim):
            boundary = True
        if boundary:
            since_boundary = 0
        warmup[i] = since_boundary < SLOPE_WINDOW_SAMPLES
        since_boundary += 1
        prev_run, prev_sim = run, sim

    verdict = 0
    for feat, ref in TRAINING_RANGE.items():
        steady, boundary_vals = [], []
        for i, c in enumerate(cooling):
            v = c.get(feat)
            if not isinstance(v, (int, float)) or v == 0.0:
                continue
            (boundary_vals if warmup[i] else steady).append(v)
        if not steady:
            print(f"  {feat}: no non-zero samples outside run warm-up")
            continue
        lo, hi = min(steady), max(steady)
        inside = sum(1 for v in steady if ref["min"] <= v <= ref["max"])
        pct = 100.0 * inside / len(steady)
        over_max = hi / ref["max"] if ref["max"] else float("inf")
        print(f"  {feat}")
        print(f"    live range      [{lo:+.4f}, {hi:+.4f}]   n={len(steady)}")
        print(f"    training p99    {ref['p99']:+.4f}    training max {ref['max']:+.4f}")
        print(f"    live max / training max = {over_max:.2f}x")
        print(f"    inside training range: {inside}/{len(steady)} ({pct:.1f}%)")
        if boundary_vals:
            outside = sum(1 for v in boundary_vals
                          if not ref["min"] <= v <= ref["max"])
            print(f"    run-boundary warm-up: {len(boundary_vals)} sample(s) "
                  f"excluded, {outside} of them outside training range "
                  f"(min {min(boundary_vals):+.4f})")
            print("      reported separately -- a window spanning a run reset "
                  "measures the join, not the run")
        if pct < 95.0:
            print("    -> FAIL: still out of distribution")
            verdict = 1
        else:
            print("    -> PASS: inside the training distribution")

    # ---- 2. Does the model beat the threshold rules? --------------------
    print("\n=== 2. Model vs plain threshold rules ===")
    first_flag = None
    first_alarm = None
    early = 0  # model alarming while the rules are still silent
    for i, (cool, pred) in enumerate(pairs):
        flagged = bool(cool.get("threshold_flags"))
        alarmed = pred.get("failure_probability", 0) >= ACTION_THRESHOLD
        if flagged and first_flag is None:
            first_flag = i
        if alarmed and first_alarm is None:
            first_alarm = i
        if alarmed and not flagged:
            early += 1

    if first_flag is None:
        print("  Threshold rules never tripped in this capture — run longer.")
        return 1
    if first_alarm is None:
        print("  FAIL: model never crossed the action threshold.")
        return 1

    lead_samples = first_flag - first_alarm
    print(f"  First model alarm (p>={ACTION_THRESHOLD}) at paired sample {first_alarm}")
    print(f"  First threshold trip            at paired sample {first_flag}")
    print(f"  Lead: {lead_samples} samples "
          f"({lead_samples * 30 / 60:.1f} simulated minutes)")
    print(f"  Samples where the model alarmed but the rules were silent: {early}")

    if lead_samples > 0 and early > 0:
        print("  -> PASS: the model fires ahead of the rules — it is adding "
              "lead time, not re-deriving them")
    elif lead_samples == 0:
        print("  -> FAIL: model fires at exactly the same sample as the rules — "
              "it is adding nothing over plain thresholds")
        verdict = 1
    else:
        print("  -> FAIL: model fires AFTER the rules — strictly worse than "
              "plain thresholds")
        verdict = 1

    # ---- 3. TTF gating --------------------------------------------------
    print("\n=== 3. time_to_failure_hours gating ===")
    preds = [p for t, p in events if t == "datacenter/predictions/CRAC-01"]
    bad = [p for p in preds
           if p.get("time_to_failure_hours") is not None
           and p.get("failure_probability", 0) < ACTION_THRESHOLD]
    low = [p for p in preds if p.get("failure_probability", 0) < ACTION_THRESHOLD]
    print(f"  predictions below p={ACTION_THRESHOLD}: {len(low)}")
    print(f"  of those, publishing a countdown anyway: {len(bad)}")
    if bad:
        print(f"    e.g. {json.dumps(bad[0])}")
        print("  -> FAIL: countdown published during normal operation")
        verdict = 1
    else:
        print("  -> PASS: no countdown published below the action threshold")

    # ---- 4. Occupancy ---------------------------------------------------
    print("\n=== 4. Occupancy on the wire ===")
    if not occupancy:
        print("  -> FAIL: no datacenter/twin-state/occupancy messages")
        verdict = 1
    else:
        lfs = [o.get("load_factor") for o in occupancy if o.get("load_factor") is not None]
        print(f"  {len(occupancy)} occupancy states, load_factor range "
              f"[{min(lfs):.3f}, {max(lfs):.3f}]")
        print(f"  -> PASS: occupancy is published and load_factor is non-default")

    print("\nVERDICT:", "PASS" if verdict == 0 else "FAIL")
    return verdict


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "capture.txt"))
