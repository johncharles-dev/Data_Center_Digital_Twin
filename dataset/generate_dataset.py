"""Generates the labeled CRAC-01 training dataset described in plan
section 5.2 — this is a SEPARATE thing from sensor_simulator.py, which
is a real-time single-lifecycle demo tool with no run structure or
failure labels. This module runs many short simulated "lifecycles" in
batch (no sleeping, no MQTT) and labels each sample against its
lifecycle's outcome, which is what a supervised classifier needs.

Reproducible from RANDOM_SEED alone — nothing here needs to be
committed as a CSV (matches section 5.2's reproducibility note).

Ground-truth columns are underscore-prefixed (_bearing_wear,
_filter_load, _fault_type) so the notebook's leakage guard drops them
automatically before training — they represent simulator internal
state, not observable telemetry.
"""
import numpy as np
import pandas as pd

SAMPLE_INTERVAL_S = 30

# Baselines — same normal operating point as sensor_simulator.py's
# CRAC_BASE, so a model trained here transfers to the live stream.
BASE = {
    "fan_rpm": 3200,
    "fan_motor_current_a": 4.5,
    "fan_motor_temp_c": 65.0,
    "fan_vibration_mm_s": 1.8,
    "filter_dp_pa": 120.0,
    "airflow_cfm": 3400.0,
    "supply_air_temp_c": 18.0,
    "return_air_temp_c": 27.0,
    "compressor_load_pct": 55.0,
}

# Detection thresholds (plan section 4.3) — where an alert should fire.
MOTOR_TEMP_ALERT = 105.0
FILTER_DP_ALERT = 350.0

# Hard mechanical-failure points — where the unit actually stops, well
# past the alert threshold, so there's a real lead-time gap to measure.
MOTOR_TEMP_FAILURE = 130.0
FILTER_DP_FAILURE = 500.0

FAULT_COUNTS = {"bearing": 59, "filter": 42, "healthy": 39}  # -> 140 runs, section 5.2


def simulate_runs(seed=42, n_runs=140):
    """Returns a DataFrame of all runs concatenated. Deterministic in
    `seed` alone."""
    rng = np.random.default_rng(seed)

    fault_types = (
        ["bearing"] * FAULT_COUNTS["bearing"]
        + ["filter"] * FAULT_COUNTS["filter"]
        + ["healthy"] * FAULT_COUNTS["healthy"]
    )
    rng.shuffle(fault_types)
    fault_types = fault_types[:n_runs]  # allow n_runs override, still shuffled

    runs = [_simulate_one_run(rng, run_id=i, fault_type=ft) for i, ft in enumerate(fault_types)]
    return pd.concat(runs, ignore_index=True)


def _simulate_one_run(rng, run_id, fault_type):
    # Median time-to-failure ~8 hours (section 5.3), log-normal spread,
    # clipped to a believable window. Healthy runs just run out the clock.
    if fault_type == "healthy":
        duration_s = rng.uniform(10 * 3600, 15 * 3600)
        failure_time_s = None
    else:
        failure_time_s = np.clip(rng.lognormal(mean=np.log(8 * 3600), sigma=0.35), 2 * 3600, 14 * 3600)
        duration_s = failure_time_s  # run ends at failure

    n_samples = int(duration_s // SAMPLE_INTERVAL_S)
    t = np.arange(n_samples) * SAMPLE_INTERVAL_S

    # Diurnal, non-stationary load factor (section 5.2's stated property).
    hour_of_day = (t / 3600.0) % 24
    load_factor = 0.985 + 0.165 * np.sin(2 * np.pi * hour_of_day / 24)

    # Wear/load progression: linear ramp to 1.0 exactly at failure, for
    # failing runs; small mean-reverting noise around 0 for healthy runs.
    if fault_type == "bearing":
        wear = np.clip(t / failure_time_s, 0, 1) + rng.normal(0, 0.01, n_samples)
        load = np.abs(rng.normal(0, 0.03, n_samples))
    elif fault_type == "filter":
        load = np.clip(t / failure_time_s, 0, 1) + rng.normal(0, 0.01, n_samples)
        wear = np.abs(rng.normal(0, 0.03, n_samples))
    else:
        wear = np.abs(rng.normal(0, 0.04, n_samples))
        load = np.abs(rng.normal(0, 0.04, n_samples))
    wear = np.clip(wear, 0, 1.05)
    load = np.clip(load, 0, 1.05)

    noise = lambda scale: rng.normal(0, scale, n_samples)

    fan_motor_temp_c = BASE["fan_motor_temp_c"] + 65 * wear + 20 * load + noise(0.5)
    fan_vibration_mm_s = np.maximum(0, BASE["fan_vibration_mm_s"] + 4.0 * wear + 0.5 * load + noise(0.05))
    filter_dp_pa = np.maximum(0, BASE["filter_dp_pa"] + 380 * load + noise(3.0))
    airflow_cfm = np.maximum(0, BASE["airflow_cfm"] - 300 * wear - 1400 * load + noise(20) )
    fan_rpm = np.maximum(0, BASE["fan_rpm"] - 200 * wear + 100 * load + noise(20)) * load_factor
    compressor_load_pct = np.clip(
        BASE["compressor_load_pct"] + 15 * wear + 20 * load + noise(1.5), 0, 100
    ) * load_factor
    fan_motor_current_a = np.maximum(0, BASE["fan_motor_current_a"] + 1.5 * wear + 0.8 * load + noise(0.05))
    supply_air_temp_c = BASE["supply_air_temp_c"] + 0.2 * wear + noise(0.2)
    return_air_temp_c = BASE["return_air_temp_c"] + 0.3 * wear + 0.2 * load + noise(0.2)

    df = pd.DataFrame({
        "run_id": run_id,
        "t": t,
        "fan_rpm": fan_rpm.round(0),
        "fan_motor_current_a": fan_motor_current_a.round(2),
        "fan_motor_temp_c": fan_motor_temp_c.round(1),
        "fan_vibration_mm_s": fan_vibration_mm_s.round(2),
        "filter_dp_pa": filter_dp_pa.round(1),
        "airflow_cfm": airflow_cfm.round(0),
        "supply_air_temp_c": supply_air_temp_c.round(1),
        "return_air_temp_c": return_air_temp_c.round(1),
        "compressor_load_pct": compressor_load_pct.round(1),
        "load_factor": load_factor.round(3),
        "_bearing_wear": wear.round(4),
        "_filter_load": load.round(4),
        "_fault_type": fault_type,
    })

    # Labels
    if failure_time_s is not None:
        ttf_min = (failure_time_s - t) / 60.0
        df["time_to_failure_min"] = ttf_min
        df["failure_within_30"] = (ttf_min <= 30).astype(int)
        df["failure_within_60"] = (ttf_min <= 60).astype(int)
        df["failure_within_120"] = (ttf_min <= 120).astype(int)
        df["failure_within_240"] = (ttf_min <= 240).astype(int)
    else:
        df["time_to_failure_min"] = np.nan
        df["failure_within_30"] = 0
        df["failure_within_60"] = 0
        df["failure_within_120"] = 0
        df["failure_within_240"] = 0

    # Retained baseline: naive 60-second-ahead symptom forecast (2 samples
    # at 30s interval) — demonstrates the limitation the plan calls out.
    df["predicted_temp_60s"] = df["fan_motor_temp_c"].shift(-2)

    return df


if __name__ == "__main__":
    df = simulate_runs()
    print(df.shape)
    print(df["_fault_type"].value_counts())
    print("Positive rate @240min:", df["failure_within_240"].mean().round(3))
