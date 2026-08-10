"""Energy Twin — room power, PUE, and the cost of a candidate action.

Consumes rack power from the Rack Twins and CRAC telemetry from the Cooling
Twin (via the orchestrator, since this twin holds centralized state rather
than subscribing to other twins' raw telemetry directly).

CRAC ELECTRICAL POWER
---------------------
Two components, each computed from telemetry this system already publishes,
with every coefficient derived below rather than fitted or guessed.

1. FAN — affinity law on measured shaft speed.

       P_fan = P_FAN_RATED * (fan_rpm / FAN_RPM_RATED)^3

   The fan affinity law states power varies with the cube of speed at a
   fixed system curve. It is the reason a degrading CRAC is expensive well
   before it fails: holding airflow against a loading filter costs speed, and
   speed costs power cubed. Over this system's own fault profile the fan runs
   3200 -> 3450 rpm, a 7.8% speed rise that the cube law turns into a 25%
   power rise.

   P_FAN_RATED is derived from the fan power equation at the nominal duty
   point, not taken from a datasheet:

       P = Q * dp / eta
         Q   = 3400 CFM nominal airflow = 1.6045 m3/s
               (CRAC_BASE["airflow_cfm"] in sensor_simulator.py)
         dp  = 500 Pa total pressure rise
               (typical for a downflow CRAC discharging into a raised-floor
               plenum: ~150-250 Pa external static plus coil, filter and
               plenum losses)
         eta = 0.55 combined fan + motor + drive efficiency
               (belt-driven centrifugal; an EC plug fan would reach ~0.65
               and this model would then overstate fan power)

       P = 1.6045 * 500 / 0.55 = 1459 W  ->  1.46 kW at 3200 rpm

2. COMPRESSOR — linear in reported load fraction.

       P_comp = P_COMP_RATED * compressor_load_pct / 100

   Linear because the modelled unit is a fixed-speed scroll compressor that
   CYCLES to meet part load: mean electrical power over a cycle is
   proportional to run-time fraction, which is what compressor_load_pct
   reports. A variable-speed (inverter) compressor would follow a markedly
   non-linear curve and this model would misstate it — that is the main
   limitation here and it is stated in the README.

   P_COMP_RATED from rated capacity and coefficient of performance:

       Q_rated = 30 kW sensible cooling
                 (sized ~1.3x the ~23 kW peak IT load this room reaches)
       COP     = 3.0 sensible, at design conditions
                 (mid-range for a DX CRAC; high-efficiency units reach 3.5+)

       P_COMP_RATED = 30 / 3.0 = 10.0 kW

WHAT THIS REPLACES
------------------
The previous line was `compressor_load_pct * 0.05`, marked "placeholder
model". It put CRAC draw at ~2.9 kW against ~18.6 kW of IT load, i.e. PUE
1.16, which is not credible for a room cooled this way and understated the
cooling energy the ROI case is built on. It also ignored fan power entirely,
so it could not show degradation costing anything.
"""
from twins.base_twin import BaseTwin

COST_PER_KWH = 0.18  # SGD/kWh, placeholder tariff — override from config

# --- fan ---------------------------------------------------------------------
FAN_RPM_RATED = 3200.0      # nominal duty point (sensor_simulator CRAC_BASE)
NOMINAL_AIRFLOW_M3_S = 1.6045   # 3400 CFM
FAN_TOTAL_PRESSURE_PA = 500.0
FAN_COMBINED_EFFICIENCY = 0.55
P_FAN_RATED_KW = round(
    NOMINAL_AIRFLOW_M3_S * FAN_TOTAL_PRESSURE_PA / FAN_COMBINED_EFFICIENCY / 1000.0, 3
)  # 1.459 kW

# --- compressor --------------------------------------------------------------
RATED_COOLING_KW = 30.0
RATED_COP = 3.0
P_COMP_RATED_KW = RATED_COOLING_KW / RATED_COP   # 10.0 kW


def crac_power_kw(fan_rpm, compressor_load_pct):
    """CRAC electrical draw, split into its two components.

    Returns (fan_kw, compressor_kw). Either input may be None — a twin that
    has not yet seen CRAC telemetry reports 0 for that component rather than
    inventing one.
    """
    fan_kw = 0.0
    if fan_rpm:
        fan_kw = P_FAN_RATED_KW * (float(fan_rpm) / FAN_RPM_RATED) ** 3

    comp_kw = 0.0
    if compressor_load_pct:
        comp_kw = P_COMP_RATED_KW * float(compressor_load_pct) / 100.0

    return round(fan_kw, 3), round(comp_kw, 3)


class EnergyTwin(BaseTwin):
    twin_id = "energy"

    def subscriptions(self):
        return ["datacenter/racks/+", "datacenter/racks/CRAC-01"]

    def handle_message(self, topic, payload):
        # Carried from the telemetry that produced this state so a consumer
        # can discard a retained reading from a finished run — same contract
        # as the cooling and rack twins.
        if payload.get("run_id") is not None:
            self.state["run_id"] = payload["run_id"]

        if "CRAC-01" in topic:
            fan_kw, comp_kw = crac_power_kw(
                payload.get("fan_rpm"), payload.get("compressor_load_pct")
            )
            self.state["crac_fan_kw"] = fan_kw
            self.state["crac_compressor_kw"] = comp_kw
            self.state["crac_power_kw"] = round(fan_kw + comp_kw, 3)
        else:
            rack_power = self.state.setdefault("rack_power_kw", {})
            rack_id = topic.split("/")[-1]
            rack_power[rack_id] = payload.get("power_draw", 0)  # simulator field is kW

        self.state["it_power_kw"] = round(sum(self.state.get("rack_power_kw", {}).values()), 3)
        total = self.state["it_power_kw"] + self.state.get("crac_power_kw", 0)
        self.state["total_power_kw"] = round(total, 3)
        self.state["pue"] = (
            round(total / self.state["it_power_kw"], 3) if self.state["it_power_kw"] else None
        )

    def estimate_action_cost(self, extra_watts, hours=1):
        return round((extra_watts / 1000) * hours * COST_PER_KWH, 4)


if __name__ == "__main__":
    import os
    twin = EnergyTwin(broker_host=os.environ["MQTT_HOST"])
    twin.run_forever()
