"""Occupancy / Workload Twin — explains heat as load-driven vs. fault.

Publishes a load_factor consumed by Rack Twins (via orchestrator) and
Energy Twin.
"""
from twins.base_twin import BaseTwin


class OccupancyTwin(BaseTwin):
    twin_id = "occupancy"

    def subscriptions(self):
        return ["datacenter/occupancy/staff", "datacenter/occupancy/workload"]

    def handle_message(self, topic, payload):
        if topic.endswith("staff"):
            self.state["staff_present"] = payload.get("count", 0)
        elif topic.endswith("workload"):
            self.state["compute_util_per_rack"] = payload.get("util_per_rack", {})

        self.state["load_factor"] = self._load_factor()

    def _load_factor(self):
        util = self.state.get("compute_util_per_rack", {})
        if not util:
            return 0.0
        return round(sum(util.values()) / len(util), 3)


if __name__ == "__main__":
    import os
    twin = OccupancyTwin(broker_host=os.environ["MQTT_HOST"])
    twin.run_forever()
