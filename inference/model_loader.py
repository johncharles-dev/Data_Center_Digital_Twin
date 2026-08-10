"""Loads the trained CRAC-01 failure model.

Build order (matches the S2 critical path):
  1. TREND_BASELINE fallback below ships first — unblocks the
     orchestrator/MQTT wiring before any real model exists.
  2. Swap in the joblib-loaded classifier + regressor once trained
     (Wed 5 Aug deliverables).
"""
import os
import joblib

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "crac_failure_model.joblib")

# The regressor is trained ONLY on rows inside the 240-minute scored
# window (notebook cell 15: "Restrict to the scored window on both
# sides of the split"). On a healthy unit it is extrapolating far
# outside its training support, and it still returns a number: an
# observed run published time_to_failure_hours = 3.74 while
# failure_probability was 0.002. A consumer rendering that field would
# show a failure countdown during normal operation.
#
# Below this probability the regressor's output is not evidence of
# anything, so report null rather than a number nobody should act on.
# Matches orchestrator.ACTION_THRESHOLD: if the prediction is not
# actionable, it does not get a countdown either.
TTF_REPORT_THRESHOLD = 0.5


def _mode_from_flags(flags, proba):
    """Which failure mechanism the evidence points at.

    IMPORTANT: this is a RULE over the CRAC's own threshold flags, not a
    model output. The trained artefact holds a binary classifier and a
    time-to-failure regressor — there is no mode classifier in it, so
    anything claiming to be a "predicted mode" from the model would be
    invented. Consumers get `predicted_mode_basis: "rule"` alongside it so
    the distinction survives onto the wire and onto the dashboard.

    WARNING_ONLY is the state worth naming: the model has crossed its action
    threshold while no sensor has passed its limit. That is precisely the
    lead time the project exists to demonstrate, and calling it a named
    mechanism would overstate what is known at that moment.
    """
    if "bearing_overheat" in flags:
        return "FAN_MOTOR_OVERHEAT"
    if "filter_restriction" in flags or "airflow_loss" in flags:
        return "FILTER_BLOCKED"
    if proba >= TTF_REPORT_THRESHOLD:
        return "WARNING_ONLY"
    return "NONE"


class CRACFailureModel:
    def __init__(self):
        self.model = None
        if os.path.exists(MODEL_PATH):
            self.model = joblib.load(MODEL_PATH)

    def predict(self, cooling_state):
        if self.model is None:
            return self._trend_baseline(cooling_state)

        features = self._feature_vector(cooling_state)
        proba = float(self.model["classifier"].predict_proba([features])[0][1])

        # Only run/report the regressor where it has training support.
        if proba >= TTF_REPORT_THRESHOLD:
            ttf_min = self.model["regressor"].predict([features])[0]  # minutes, not hours
            ttf_hours = round(float(ttf_min) / 60.0, 2)
        else:
            ttf_hours = None

        flags = cooling_state.get("threshold_flags", [])
        return {
            "failure_probability": round(proba, 3),
            "time_to_failure_hours": ttf_hours,
            "contributing_factors": flags,
            "predicted_mode": _mode_from_flags(flags, proba),
            "predicted_mode_basis": "rule",
        }

    def _feature_vector(self, s):  # noqa: E301
        # Built BY NAME from the model's own feature_cols, saved at
        # training time — not hardcoded here. A hardcoded list here is
        # exactly how this project ended up with a 7-vs-11 feature
        # mismatch the first time around; this makes that class of bug
        # impossible unless the twin's state is missing a field.
        missing = [c for c in self.model["feature_cols"] if c not in s]
        if missing:
            raise KeyError(
                f"CoolingTwin state is missing features the model needs: {missing}. "
                f"Available: {list(s.keys())}"
            )
        return [s[c] for c in self.model["feature_cols"]]

    def _trend_baseline(self, s):
        """Rule-based stand-in used until the real model is trained."""
        flags = s.get("threshold_flags", [])
        prob = 0.9 if flags else 0.1
        return {
            "failure_probability": prob,
            "time_to_failure_hours": None,
            "contributing_factors": flags,
            "predicted_mode": _mode_from_flags(flags, prob),
            "predicted_mode_basis": "rule",
        }
