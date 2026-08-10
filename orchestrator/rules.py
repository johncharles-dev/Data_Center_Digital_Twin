"""Rule set mapping risk to proportionate response (plan section 6).

Kept as plain if/elif on purpose — this must stay auditable per the
governance requirement in section 9.
"""

def recommend_action(prediction, cooling_state, energy_state):
    prob = prediction["failure_probability"]
    flags = cooling_state.get("threshold_flags", [])

    if "bearing_overheat" in flags:
        action = "schedule_fan_bearing_inspection"
        rationale = "Motor temperature at/above Class F alarm point."
    elif "filter_restriction" in flags or "airflow_loss" in flags:
        action = "flag_filter_replacement"
        rationale = "Filter pressure drop or airflow loss beyond spec."
    elif prob >= 0.7:
        action = "reduce_compute_load_on_affected_racks"
        rationale = "High failure probability without a specific sensor trip yet."
    else:
        action = "monitor_increase_polling"
        rationale = "Elevated but sub-critical failure probability."

    return {
        "action": action,
        "rationale": rationale,
        "failure_probability": prob,
        "time_to_failure_hours": prediction.get("time_to_failure_hours"),
        "estimated_cost": _estimate_cost(action, energy_state),
    }


def _estimate_cost(action, energy_state):
    # Placeholder — wire up to EnergyTwin.estimate_action_cost equivalents
    # once the real cost model lands (S4 dependency).
    return None
