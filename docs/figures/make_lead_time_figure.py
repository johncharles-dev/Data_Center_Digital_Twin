"""Regenerates the lead-time comparison figure used in the report.

Reproduces the training notebook's evaluation exactly — same dataset seed,
same feature construction, same grouped/stratified split, same lead-time
function — and scores the COMMITTED model artefact rather than retraining, so
the figure describes the model that actually ships.

The script asserts its own numbers against the notebook's committed output
(292.5 / 195.2 minutes) and refuses to write the figure if they drift. A plot
that quietly disagrees with the report's headline number would be worse than
no plot.

    python3 docs/figures/make_lead_time_figure.py
"""
import os
import sys

import joblib
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from dataset.generate_dataset import simulate_runs
from sklearn.model_selection import ShuffleSplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(HERE, "lead_time_comparison.png")

RANDOM_SEED = 42
ROLL_WINDOW = 20                # 10 minutes at a 30s sample interval
FEATURE_COLS = [
    "fan_rpm", "fan_motor_current_a", "fan_motor_temp_c", "fan_vibration_mm_s",
    "filter_dp_pa", "airflow_cfm", "supply_air_temp_c", "return_air_temp_c",
    "compressor_load_pct", "motor_temp_slope_10min", "filter_dp_slope_10min",
]
# The same plan section 4.3 limits the CoolingTwin trips on.
MOTOR_TEMP_ALERT, FILTER_DP_ALERT = 105.0, 350.0
NOMINAL_AIRFLOW_CFM, AIRFLOW_TRIP_PCT = 3400.0, 0.65

EXPECTED_MODEL, EXPECTED_BASELINE = 292.5, 195.2


def lead_times(df, alert_mask):
    """Per-run lead time in minutes, across FAILING runs only.

    A run the detector never caught is excluded rather than scored as zero —
    that flatters BOTH detectors, so the coverage counts are reported next to
    the medians.
    """
    failing = df[df["time_to_failure_min"].notna()].copy()
    failing["alert"] = alert_mask[failing.index]
    out = []
    for _, run_df in failing.groupby("run_id"):
        alerted = run_df[run_df["alert"]]
        if not alerted.empty:
            out.append(alerted["time_to_failure_min"].max())
    return out, failing["run_id"].nunique()


def main():
    df = simulate_runs(seed=RANDOM_SEED, n_runs=140).sort_values(["run_id", "t"])
    for col, feat in (("fan_motor_temp_c", "motor_temp_slope_10min"),
                      ("filter_dp_pa", "filter_dp_slope_10min")):
        df[feat] = df.groupby("run_id")[col].transform(
            lambda s: s.diff(ROLL_WINDOW) / ROLL_WINDOW)

    leak = [c for c in df.columns if c.startswith("_") and c != "_fault_type"]
    df = df.drop(columns=leak).dropna(subset=FEATURE_COLS + ["failure_within_240"])

    splitter = ShuffleSplit(n_splits=1, test_size=0.30, random_state=RANDOM_SEED)
    test_ids = []
    for _, group in df.groupby("_fault_type"):
        ids = group["run_id"].unique()
        _, te = next(splitter.split(ids))
        test_ids.extend(ids[te])
    test_df = df[df["run_id"].isin(test_ids)]

    clf = joblib.load(os.path.join(ROOT, "models", "crac_failure_model.joblib"))["classifier"]
    model_alert = pd.Series(
        clf.predict_proba(test_df[FEATURE_COLS])[:, 1] >= 0.5, index=test_df.index)
    threshold_alert = (
        (test_df["fan_motor_temp_c"] >= MOTOR_TEMP_ALERT)
        | (test_df["filter_dp_pa"] >= FILTER_DP_ALERT)
        | (test_df["airflow_cfm"] <= NOMINAL_AIRFLOW_CFM * AIRFLOW_TRIP_PCT))

    model_lt, n_failing = lead_times(test_df, model_alert)
    base_lt, _ = lead_times(test_df, threshold_alert)
    m_med, b_med = round(float(np.median(model_lt)), 1), round(float(np.median(base_lt)), 1)

    print(f"failing test runs: {n_failing}")
    print(f"model     median {m_med} min  (caught {len(model_lt)}/{n_failing})")
    print(f"threshold median {b_med} min  (caught {len(base_lt)}/{n_failing})")
    print(f"multiple: {round(m_med / b_med, 2)}x")

    # Refuse to publish a figure that disagrees with the committed notebook.
    for got, want, label in ((m_med, EXPECTED_MODEL, "model"),
                             (b_med, EXPECTED_BASELINE, "threshold")):
        if abs(got - want) > 0.1:
            raise SystemExit(
                f"ABORT: {label} median {got} does not reproduce the notebook's "
                f"{want}. The figure was not written.")

    # ---------------------------------------------------------------- figure
    ink, grid = "#17252E", "#D6DEE4"
    model_c, base_c = "#1C7293", "#D97706"
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=170)
    fig.patch.set_facecolor("white"); ax.set_facecolor("white")

    rng = np.random.default_rng(RANDOM_SEED)
    for vals, y, colour, label in ((base_lt, 1.0, base_c, "Threshold rules"),
                                   (model_lt, 2.0, model_c, "Model, p ≥ 0.5")):
        jitter = y + rng.uniform(-0.11, 0.11, len(vals))
        ax.scatter(vals, jitter, s=26, alpha=0.55, color=colour,
                   edgecolors="none", zorder=3)
        med = float(np.median(vals))
        ax.plot([med, med], [y - 0.26, y + 0.26], color=colour, lw=2.6, zorder=4)
        ax.text(med, y + 0.33, f"median {med:.1f} min", color=colour,
                ha="center", fontsize=9.5, fontweight="600")

    ax.annotate("", xy=(m_med, 1.62), xytext=(b_med, 1.62),
                arrowprops=dict(arrowstyle="<->", color=ink, lw=1.3))
    ax.text((m_med + b_med) / 2, 1.68, f"{m_med - b_med:.1f} min more warning",
            ha="center", fontsize=9.5, color=ink, fontweight="600")

    ax.set_yticks([1.0, 2.0]); ax.set_yticklabels(["Threshold rules", "Model"], fontsize=10.5)
    ax.set_xlabel("Lead time before failure (minutes) — one point per failing test run",
                  fontsize=10, color=ink)
    ax.set_title(f"Warning before failure, {n_failing} failing test runs "
                 f"· both detectors caught all {n_failing}",
                 fontsize=12, fontweight="600", color=ink, pad=14)
    ax.grid(axis="x", color=grid, lw=0.8); ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.set_ylim(0.55, 2.5); ax.tick_params(colors=ink, length=0)
    fig.tight_layout()
    fig.savefig(OUT, facecolor="white")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
