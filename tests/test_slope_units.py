"""Asserts the live slope features are in the SAME UNIT the model was
trained on.

The notebook (cell 5) computes both slope features as
    s.diff(ROLL_WINDOW) / ROLL_WINDOW      # ROLL_WINDOW = 20
on a dataset sampled every 30s, i.e. value change per 30-second sample.

CoolingTwin._slope computes the same quantity from timestamped
readings. If the two ever disagree the model is being fed a feature in
a unit it has never seen, which is exactly the defect this test exists
to prevent regressing.

Run:  python3 tests/test_slope_units.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataset.generate_dataset import SAMPLE_INTERVAL_S
from twins.cooling_twin import (
    CoolingTwin, TRAINING_SAMPLE_INTERVAL_S, SLOPE_WINDOW_SECONDS,
    MIN_WINDOW_FRACTION)

ROLL_WINDOW = 20  # identical to the notebook


def notebook_slope(values):
    """Exactly the notebook's cell-5 expression, final value."""
    s = pd.Series(values)
    return float((s.diff(ROLL_WINDOW) / ROLL_WINDOW).iloc[-1])


def twin_slope(values, interval_s=SAMPLE_INTERVAL_S):
    """CoolingTwin._slope over the same readings."""
    history = [(i * interval_s, v) for i, v in enumerate(values)]
    return CoolingTwin._slope(history)


def check(name, values, interval_s=SAMPLE_INTERVAL_S):
    want = notebook_slope(values)
    got = twin_slope(values, interval_s)
    ok = abs(want - got) < 1e-3
    print(f"  {'PASS' if ok else 'FAIL'}  {name:32s} notebook={want:+.5f}  twin={got:+.5f}")
    assert ok, f"{name}: notebook={want} twin={got} — UNIT MISMATCH"
    return ok


def main():
    print("Training sample interval:", SAMPLE_INTERVAL_S, "s")
    print("Twin's assumed interval: ", TRAINING_SAMPLE_INTERVAL_S, "s")
    assert TRAINING_SAMPLE_INTERVAL_S == SAMPLE_INTERVAL_S, (
        "CoolingTwin.TRAINING_SAMPLE_INTERVAL_S must match the dataset "
        "generator's SAMPLE_INTERVAL_S")

    n = ROLL_WINDOW + 1  # exactly one full window

    print("\nSlope agreement over one full 20-sample window:")
    # Linear ramps of several gradients, matching real feature scales.
    check("motor temp +0.07/sample", [65.0 + 0.07 * i for i in range(n)])
    check("motor temp +0.25/sample", [65.0 + 0.25 * i for i in range(n)])
    check("filter dp  +1.40/sample", [120.0 + 1.40 * i for i in range(n)])
    check("falling    -0.30/sample", [90.0 - 0.30 * i for i in range(n)])
    check("flat        0.00/sample", [65.0] * n)

    # Non-linear: both are endpoint-to-endpoint, so they must still agree.
    rng = np.random.default_rng(0)
    noisy = list(65.0 + 0.1 * np.arange(n) + rng.normal(0, 0.5, n))
    check("noisy ramp", noisy)

    # The real point of the fix: a run whose wall-clock cadence differs
    # from the training cadence must still yield the training-unit slope.
    print("\nSame physical degradation, different publish cadences:")
    deg_per_second = 0.07 / SAMPLE_INTERVAL_S  # 0.07 per 30s sample
    for cadence in (1.0, 5.0, 30.0, 60.0):
        # Enough samples to span the full 600s window at this cadence —
        # below that the MIN_WINDOW_FRACTION gate deliberately reports
        # 0.0 (covered separately by the partial-window check below).
        span_n = int(SLOPE_WINDOW_SECONDS / cadence) + 1
        vals = [65.0 + deg_per_second * cadence * i for i in range(span_n)]
        got = twin_slope(vals, interval_s=cadence)
        ok = abs(got - 0.07) < 1e-3
        print(f"  {'PASS' if ok else 'FAIL'}  cadence {cadence:5.1f}s -> "
              f"slope={got:+.5f} (want +0.07000)")
        assert ok, f"cadence {cadence}s produced {got}, expected 0.07"

    # A partial window must report 0.0, not a short-window slope the
    # model has never seen (the notebook drops those rows entirely).
    print("\nPartial-window gate:")
    partial = [(i * 1.0, 65.0 + 0.5 * i) for i in range(30)]  # 29s span
    got = CoolingTwin._slope(partial)
    ok = got == 0.0
    print(f"  {'PASS' if ok else 'FAIL'}  29s span (< {SLOPE_WINDOW_SECONDS * MIN_WINDOW_FRACTION:.0f}s) -> {got}")
    assert ok, f"partial window returned {got}, expected 0.0"

    print("\nAll slope-unit assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
