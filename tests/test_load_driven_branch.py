"""The load-vs-fault branch: proof it was unreachable, and proof the
replacement is reachable.

Original predicate (orchestrator/orchestrator.py):

    is_load_driven = load_factor > 0.8 and failure_probability < 0.3
    if failure_probability >= 0.5 and not is_load_driven:
        ...recommend...

`is_load_driven` is only ever READ inside a branch already guarded by
failure_probability >= 0.5, and it requires failure_probability < 0.3.
The two conditions are mutually exclusive, so `is_load_driven` is False
at every point it is evaluated and the suppression can never fire — no
occupancy data, however high, could change the outcome. That is a
separate defect from "nothing publishes occupancy", and fixing the
publisher alone would not have surfaced it.

Run:  python3 tests/test_load_driven_branch.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.orchestrator import is_load_driven, should_recommend

GRID_STEP = 0.01


def old_is_load_driven(load_factor, prob):
    """The predicate as it shipped."""
    return load_factor > 0.8 and prob < 0.3


def sweep(predicate):
    """Exhaustive sweep of the reachable input space. Returns the number
    of (load_factor, prob) pairs where suppression actually fires — i.e.
    the model wants to alarm but occupancy explains the heat away."""
    fired = 0
    total = 0
    steps = int(1.0 / GRID_STEP) + 1
    for i in range(steps):
        load = round(i * GRID_STEP, 2)
        for j in range(steps):
            prob = round(j * GRID_STEP, 2)
            if prob < 0.5:
                continue  # the outer guard never reaches the predicate here
            total += 1
            if predicate(load, prob):
                fired += 1
    return fired, total


def main():
    print("Sweeping load_factor x failure_probability on a 0.01 grid,")
    print("restricted to the region the outer guard actually reaches")
    print("(failure_probability >= 0.5).\n")

    old_fired, total = sweep(old_is_load_driven)
    print(f"  ORIGINAL predicate: suppression fires in {old_fired:5d} / {total} reachable states")
    assert old_fired == 0, (
        "expected the original predicate to be unreachable; it fired "
        f"{old_fired} times — the defect description is wrong")
    print("    -> confirmed unreachable: occupancy could never affect the outcome")

    new_fired, total = sweep(lambda load, prob: is_load_driven(load, {"threshold_flags": []}))
    print(f"  FIXED    predicate: suppression fires in {new_fired:5d} / {total} reachable states")
    assert new_fired > 0, "the replacement predicate is still unreachable"
    print("    -> reachable")

    print("\nBehavioural checks on the fixed predicate:")
    cases = [
        # (load_factor, cooling_state, want_suppressed, description)
        (0.95, {"threshold_flags": []}, True,
         "high load, no equipment trip -> heat is load-driven, suppress"),
        (0.95, {"threshold_flags": ["bearing_overheat"]}, False,
         "high load BUT bearing tripped -> real fault, do not suppress"),
        (0.20, {"threshold_flags": []}, False,
         "low load, no trip -> not explained by load, do not suppress"),
        (0.20, {"threshold_flags": ["filter_restriction"]}, False,
         "low load, filter tripped -> real fault, do not suppress"),
    ]
    ok = True
    for load, cooling, want, desc in cases:
        got = is_load_driven(load, cooling)
        good = got == want
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  load={load:.2f} flags={cooling['threshold_flags'] or '[]'!s:24s} "
              f"suppressed={got}  ({desc})")
        assert good, f"{desc}: got {got}, want {want}"

    print("\nEnd-to-end guard (should_recommend):")
    checks = [
        (0.60, 0.95, {"threshold_flags": []}, False,
         "model alarms but high load explains it -> no recommendation"),
        (0.60, 0.95, {"threshold_flags": ["bearing_overheat"]}, True,
         "model alarms, equipment tripped -> recommend"),
        (0.60, 0.10, {"threshold_flags": []}, True,
         "model alarms, load is low -> recommend"),
        (0.40, 0.95, {"threshold_flags": []}, False,
         "model below action threshold -> no recommendation"),
    ]
    for prob, load, cooling, want, desc in checks:
        got = should_recommend(prob, load, cooling)
        good = got == want
        print(f"  {'PASS' if good else 'FAIL'}  prob={prob:.2f} load={load:.2f} -> recommend={got}  ({desc})")
        assert good, f"{desc}: got {got}, want {want}"

    print("\nAll load-vs-fault assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
