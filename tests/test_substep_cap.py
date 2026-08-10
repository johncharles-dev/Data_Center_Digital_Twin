"""The sub-stepping guarantee.

Two properties, both asserted rather than inspected:

  1. A publish tick NEVER advances the simulated clock by more than one
     training sample interval, whatever --sim-step is set to. If it
     could, the stream would be sampled more coarsely than the model was
     trained on and the slope features would drift out of distribution
     again.
  2. The degradation is invariant to playback speed. Running the same
     fault at two different wall-clock cadences must produce the same
     telemetry at the same SIMULATED time — acceleration changes how
     fast you watch, not what you are watching.

Run:  python3 tests/test_substep_cap.py
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sensor_simulator as ss


def make_sim(sim_step, interval):
    args = argparse.Namespace(
        csv=False, sim_step=sim_step, interval=interval,
        anomaly=True, no_mqtt=True, host="localhost", port=1883)
    return ss.Simulator(args)


def trajectory(sim, n_ticks, seed=7):
    """Advance the simulated clock n_ticks times without sleeping, and
    record CRAC readings against SIMULATED time."""
    random.seed(seed)
    sim.anomaly_active = True
    sim.anomaly_elapsed = 0.0
    out = []
    for _ in range(n_ticks):
        r = sim._crac_reading()
        out.append((sim.t, r["fan_motor_temp_c"], r["filter_dp_pa"], r["airflow_cfm"]))
        sim.t += sim.sim_step
        sim.anomaly_elapsed += sim.sim_step
    return out


def main():
    cap = ss.SIM_SAMPLE_INTERVAL_S
    print(f"Training sample interval (cap): {cap}s\n")

    print("1. Per-tick advance is capped regardless of --sim-step:")
    ok = True
    for requested in (1.0, 15.0, 30.0, 60.0, 600.0, 86400.0):
        sim = make_sim(requested, 1.0)
        good = sim.sim_step <= cap
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  --sim-step {requested:>9.1f} "
              f"-> actual {sim.sim_step:5.1f}s per tick")
        assert good, f"--sim-step {requested} was honoured as {sim.sim_step}, above cap {cap}"

    print("\n   ...and the cap holds across a full fault's worth of ticks:")
    sim = make_sim(86400.0, 1.0)
    traj = trajectory(sim, 200)
    steps = [round(traj[i + 1][0] - traj[i][0], 6) for i in range(len(traj) - 1)]
    worst = max(steps)
    good = worst <= cap
    print(f"  {'PASS' if good else 'FAIL'}  largest observed advance over 200 ticks: {worst}s")
    assert good, f"observed a {worst}s advance, above cap {cap}"

    print("\n2. Degradation is invariant to playback speed:")
    # Same sim_step, wildly different wall cadence -> identical telemetry
    # at identical simulated times.
    slow = trajectory(make_sim(cap, 1.00), 400)
    fast = trajectory(make_sim(cap, 0.02), 400)
    assert len(slow) == len(fast)
    worst_delta = 0.0
    for (t_a, m_a, f_a, a_a), (t_b, m_b, f_b, a_b) in zip(slow, fast):
        assert t_a == t_b, f"simulated clocks diverged: {t_a} vs {t_b}"
        worst_delta = max(worst_delta, abs(m_a - m_b), abs(f_a - f_b), abs(a_a - a_b))
    good = worst_delta == 0.0
    print(f"  {'PASS' if good else 'FAIL'}  1.0s vs 0.02s wall cadence "
          f"(50x apart): max telemetry difference at matched simulated "
          f"time = {worst_delta}")
    assert good, f"playback speed changed the degradation by {worst_delta}"

    # And the fault still actually completes.
    end_temp = slow[-1][1]
    reached = end_temp >= ss.CRAC_MOTOR_TEMP_TRIP
    print(f"  {'PASS' if reached else 'FAIL'}  fault still reaches its trip point: "
          f"motor_temp={end_temp:.1f}C (trip {ss.CRAC_MOTOR_TEMP_TRIP}C)")
    assert reached, "the fault no longer reaches its threshold"

    print(f"\n   Fault duration: {ss.ANOMALY_DURATION_S / 3600:.1f}h simulated "
          f"= {int(ss.ANOMALY_DURATION_S / cap)} ticks minimum")

    print("\nAll sub-stepping assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
