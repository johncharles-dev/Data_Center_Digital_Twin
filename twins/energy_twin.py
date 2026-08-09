"""Energy Twin — cost of each candidate recommended action.

Consumes power from Rack Twins and compressor load from Cooling Twin
(via the orchestrator, since this twin is centralized state, not a
direct subscriber to other twins' raw telemetry).
"""
from twins.base_twin import BaseTwin

COST_PER_KWH = 0.18  # placeholder tariff, override from config


class EnergyTwin(BaseTwin):
    twin_id = "energy"

    def subscriptions(self):
        return ["datacenter/racks/+", "datacenter/racks/CRAC-01"]

    def handle_message(self, topic, payload):
        if "CRAC-01" in topic:
            self.state["crac_power_kw"] = payload.get("compressor_load_pct", 0) * 0.05  # placeholder model
        else:
            rack_power = self.state.setdefault("rack_power_kw", {})
            rack_id = topic.split("/")[-1]
            rack_power[rack_id] = payload.get("power_draw", 0)  # simulator field is power_draw (kW)

        self.state["it_power_kw"] = sum(self.state.get("rack_power_kw", {}).values())
        total = self.state["it_power_kw"] + self.state.get("crac_power_kw", 0)
        self.state["total_power_kw"] = total
        self.state["pue"] = (
            round(total / self.state["it_power_kw"], 3) if self.state["it_power_kw"] else None
        )

    def estimate_action_cost(self, extra_watts, hours=1):
        return round((extra_watts / 1000) * hours * COST_PER_KWH, 4)


if __name__ == "__main__":
    import os
    twin = EnergyTwin(broker_host=os.environ["MQTT_HOST"])
    twin.run_forever()
