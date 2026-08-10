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


class CRACFailureModel:
    def __init__(self):
        self.model = None
        if os.path.exists(MODEL_PATH):
            self.model = joblib.load(MODEL_PATH)

    def predict(self, cooling_state):
        if self.model is None:
            return self._trend_baseline(cooling_state)

        features = self._feature_vector(cooling_state)
        proba = self.model["classifier"].predict_proba([features])[0][1]
        ttf_min = self.model["regressor"].predict([features])[0]  # trained on minutes, not hours
        return {
            "failure_probability": round(float(proba), 3),
            "time_to_failure_hours": round(float(ttf_min) / 60.0, 2),
            "contributing_factors": cooling_state.get("threshold_flags", []),
        }

    def _feature_vector(self, s):
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
        }
