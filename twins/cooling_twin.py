"""Cooling Twin — CRAC-01 fan/motor/filter/airflow state.

Failure thresholds per plan section 4.3:
  fan_motor_temp_c >= 105   -> bearing wear -> overheating
  filter_dp_pa     >= 350   -> filter clogging -> restriction
  airflow_cfm      <= 65% nominal (nominal 3400) -> airflow loss
"""
import time
from collections import deque
from datetime import datetime, timezone

from twins.base_twin import BaseTwin

NOMINAL_AIRFLOW_CFM = 3400
AIRFLOW_TRIP_PCT = 0.65

# The training notebook computes both slope features as
#   df.groupby("run_id")[col].transform(lambda s: s.diff(20) / 20)
# on a dataset sampled every 30s. The numerator is the value change
# across 20 samples (= 600s) and the denominator is 20 SAMPLES, so the
# unit is "value change per 30-second sample" — NOT per minute.
#
# An earlier version of this file divided by elapsed MINUTES, which is
# the same physical quantity expressed in a unit 2x larger, so every
# live slope reached the model inflated 2x. Inference must produce the
# unit the model was trained on, so the divisor here is the elapsed
# span expressed in training samples.
TRAINING_SAMPLE_INTERVAL_S = 30.0
SLOPE_WINDOW_SECONDS = 600.0  # 10 minutes == 20 training samples

# diff(20) is NaN until 20 samples exist, and the notebook drops those
# rows before training, so the model has never seen a slope computed
# over a partial window. Report 0.0 until the buffer spans (almost) the
# full window rather than feeding it a short-window slope it can't
# interpret.
MIN_WINDOW_FRACTION = 0.9


def _payload_time(payload):
    """Seconds-since-epoch for a reading, taken from the payload's own
    timestamp rather than wall clock.

    The simulator advances a SIMULATED clock that can run faster than
    wall clock (see sensor_simulator.py --sim-step). Measuring slope
    against wall clock would divide a simulated-hours-worth of change
    by a wall-clock-seconds elapsed time and inflate it by exactly the
    acceleration factor. The payload timestamp is the only clock that
    means the same thing live as it did during training.
    """
    ts = payload.get("timestamp")
    if not ts:
        return time.time()  # no timestamp in payload — fall back
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return time.time()


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
        # Carry the SIMULATED clock forward into twin state. Consumers that
        # plot or measure elapsed time need the same clock the slopes were
        # measured against — wall clock is meaningless here, because a tick
        # advances simulated time by up to 30s regardless of how fast the
        # playback runs.
        sim_time = _payload_time(payload)
        self.state["sim_time"] = sim_time
        self.state["timestamp"] = payload.get("timestamp")
        # Which degradation run this state belongs to. Carried through so a
        # consumer can tell a retained message from a finished run apart from
        # current state — see sensor_simulator.py's run_id.
        self.state["run_id"] = payload.get("run_id")

        self._record_history(sim_time)
        self.state["motor_temp_slope_10min"] = self._slope(self._motor_temp_history)
        self.state["filter_dp_slope_10min"] = self._slope(self._filter_dp_history)
        self.state["cooling_effectiveness"] = self._effectiveness()
        self.state["threshold_flags"] = self._check_thresholds()
        # failure_risk / time_to_failure are filled in by the trained
        # model in inference/model_loader.py, not here — this twin only
        # tracks raw + rule-based state.

    def _record_history(self, now):
        cutoff = now - SLOPE_WINDOW_SECONDS

        self._motor_temp_history.append((now, self.state.get("fan_motor_temp_c")))
        self._filter_dp_history.append((now, self.state.get("filter_dp_pa")))

        for hist in (self._motor_temp_history, self._filter_dp_history):
            while hist and hist[0][0] < cutoff:
                hist.popleft()

    @staticmethod
    def _slope(history):
        """Value change per 30-second training sample over the window.

        Unit-identical to the notebook's s.diff(20)/20: that divides the
        change across a 600s span by 20 samples, and this divides the
        change across the buffer's span by that span expressed in
        30-second samples. tests/test_slope_units.py asserts the two
        agree numerically.
        """
        if len(history) < 2:
            return 0.0
        t0, v0 = history[0]
        t1, v1 = history[-1]
        elapsed_s = t1 - t0
        if elapsed_s < SLOPE_WINDOW_SECONDS * MIN_WINDOW_FRACTION:
            return 0.0  # window not full yet — see MIN_WINDOW_FRACTION
        if v0 is None or v1 is None:
            return 0.0
        elapsed_samples = elapsed_s / TRAINING_SAMPLE_INTERVAL_S
        return round((v1 - v0) / elapsed_samples, 4)

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
