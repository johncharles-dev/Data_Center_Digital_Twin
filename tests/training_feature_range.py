"""Prints the range of the two engineered slope features across the
training set, exactly as the notebook computes them.

These are the numbers TRAINING_RANGE in tests/analyse_live_run.py is
checked against — regenerate them here rather than trusting a constant
if the dataset generator ever changes.

Usage:  python3 tests/training_feature_range.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataset.generate_dataset import simulate_runs

ROLL_WINDOW = 20  # notebook cell 5


def main():
    df = simulate_runs(seed=42, n_runs=140).sort_values(["run_id", "t"])
    print(f"{len(df)} rows, {df['run_id'].nunique()} runs\n")
    for col, feat in (("fan_motor_temp_c", "motor_temp_slope_10min"),
                      ("filter_dp_pa", "filter_dp_slope_10min")):
        s = df.groupby("run_id")[col].transform(
            lambda x: x.diff(ROLL_WINDOW) / ROLL_WINDOW).dropna()
        print(f"{feat}:")
        print(f"  min  {s.min():+.4f}")
        print(f"  p50  {s.quantile(0.50):+.4f}")
        print(f"  p99  {s.quantile(0.99):+.4f}")
        print(f"  max  {s.max():+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
