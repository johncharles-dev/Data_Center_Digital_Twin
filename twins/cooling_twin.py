"""Cooling Twin — CRAC-01 fan/motor/filter/airflow state.

Failure thresholds per plan section 4.3:
  fan_motor_temp_c >= 105   -> bearing wear -> overheating
  filter_dp_pa     >= 350   -> filter clogging -> restriction
  airflow_cfm      <= 65% nominal (nominal 3400) -> airflow loss
"""
import time
from collections import deque
from twins.base_twin import BaseTwin

NOMINAL_AIRFLOW_CFM = 3400
AIRFLOW_TRIP_PCT = 0.65

# The training notebook used a 20-sample window at a 30s sample
# interval = 10 minutes. Tracked here by WALL-CLOCK TIME instead of
# sample count, since sensor_simulator.py publishes at a different
# interval (1s) than the training data was generated at — a
# count-based window would silently mean something different live
# than it did during training.
SLOPE_WINDOW_SECONDS = 600  # 10 minutes


class CoolingTwin(BaseTwin):
    twin_id = "cooling"

    def __init__(self, *args, **kwargs):
        self._motor_temp_history = deque()  # (timestamp, value)
        self._filter_dp_history = deque()
        super().__init__(*args, **kwargs)

    def subscriptions(self):
        return ["datacenter/racks/CRAC-01"]

    def handle_message(self, topic, payload):
        self.state.update({
            "fan_rpm": payload.get("fan_rpm"),
            "fan_motor_current_a": payload.get("fan_motor_current_a"),
            "fan_motor_temp_c": payload.get("fan_motor_temp_c"),
            "fan_vibration_mm_s": payload.get("fan_vibration_mm_s"),
            "filter_dp_pa": payload.get("filter_dp_pa"),
            "airflow_cfm": payload.get("airflow_cfm"),
            "supply_air_temp_c": payload.get("supply_air_temp_c"),
            "return_air_temp_c": payload.get("return_air_temp_c"),
            "compressor_load_pct": payload.get("compressor_load_pct"),
        })
        self._record_history()
        self.state["motor_temp_slope_10min"] = self._slope(self._motor_temp_history)
        self.state["filter_dp_slope_10min"] = self._slope(self._filter_dp_history)
        self.state["cooling_effectiveness"] = self._effectiveness()
        self.state["threshold_flags"] = self._check_thresholds()
        # failure_risk / time_to_failure are filled in by the trained
        # model in inference/model_loader.py, not here — this twin only
        # tracks raw + rule-based state.

    def _record_history(self):
        now = time.time()
        cutoff = now - SLOPE_WINDOW_SECONDS

        self._motor_temp_history.append((now, self.state.get("fan_motor_temp_c")))
        self._filter_dp_history.append((now, self.state.get("filter_dp_pa")))

        for hist in (self._motor_temp_history, self._filter_dp_history):
            while hist and hist[0][0] < cutoff:
                hist.popleft()

    @staticmethod
    def _slope(history):
        """Value change per minute over the window. Matches the sign
        and units of motor_temp_slope_10min / filter_dp_slope_10min
        in the training notebook (value delta / elapsed minutes)."""
        if len(history) < 2:
            return 0.0
        t0, v0 = history[0]
        t1, v1 = history[-1]
        elapsed_min = (t1 - t0) / 60.0
        if elapsed_min <= 0 or v0 is None or v1 is None:
            return 0.0
        return round((v1 - v0) / elapsed_min, 4)

    def _effectiveness(self):
        airflow = self.state.get("airflow_cfm") or 0
        return round(airflow / NOMINAL_AIRFLOW_CFM, 3)

    def _check_thresholds(self):
        flags = []
        if (self.state.get("fan_motor_temp_c") or 0) >= 105:
            flags.append("bearing_overheat")
        if (self.state.get("filter_dp_pa") or 0) >= 350:
            flags.append("filter_restriction")
        if self._effectiveness() <= AIRFLOW_TRIP_PCT:
            flags.append("airflow_loss")
        return flags


if __name__ == "__main__":
    import os
    twin = CoolingTwin(broker_host=os.environ["MQTT_HOST"])
    twin.run_forever()
