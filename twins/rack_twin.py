"""Rack Twin — one instance per rack (SR-RACK-01/02/03).

Tracks thermal state and a rolling slope used as a short-horizon
transparent baseline forecast (section 5, retained as comparison
against the CRAC-01 predictive model).
"""
from collections import deque
from twins.base_twin import BaseTwin

SLOPE_WINDOW = 12  # readings to keep for rolling slope


class RackTwin(BaseTwin):
    def __init__(self, rack_id, *args, **kwargs):
        self.twin_id = f"rack-{rack_id}"
        self.rack_id = rack_id
        self._exhaust_history = deque(maxlen=SLOPE_WINDOW)
        super().__init__(*args, **kwargs)

    def subscriptions(self):
        return [f"datacenter/racks/{self.rack_id}"]

    def handle_message(self, topic, payload):
        # field names match sensor_simulator.py's actual payload, not the
        # earlier _c/_w-suffixed guesses
        inlet = payload.get("inlet_temperature")
        exhaust = payload.get("exhaust_temperature")
        self._exhaust_history.append(exhaust)

        self.state.update({
            "rack_id": self.rack_id,
            "location": payload.get("location"),
            "run_id": payload.get("run_id"),
            "inlet_temperature": inlet,
            "exhaust_temperature": exhaust,
            "delta_t": None if None in (inlet, exhaust) else round(exhaust - inlet, 2),
            "fan_speed": payload.get("fan_speed"),
            "power_draw_kw": payload.get("power_draw"),  # simulator reports kW, not W
            "status": payload.get("status"),  # simulator's own NORMAL/WARNING/CRITICAL
            "exhaust_slope_per_reading": self._slope(),
        })
        # thermal_risk gets set by the orchestrator once it has
        # occupancy context (load-driven heat vs. equipment fault).

    def _slope(self):
        if len(self._exhaust_history) < 2:
            return 0.0
        vals = list(self._exhaust_history)
        return round((vals[-1] - vals[0]) / (len(vals) - 1), 3)


if __name__ == "__main__":
    import os
    import sys
    rack_id = sys.argv[1] if len(sys.argv) > 1 else "SR-RACK-01"
    twin = RackTwin(rack_id, broker_host=os.environ["MQTT_HOST"])
    twin.run_forever()
