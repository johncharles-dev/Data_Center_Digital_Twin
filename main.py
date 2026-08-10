"""Single entry point — starts all twins and the orchestrator in one
process using threads. Suitable for the always-on Render/Railway/Fly.io
instance described in section 12.1.

RUN_SIMULATOR=true also starts sensor_simulator.py in the same process
(single-service deploy: telemetry source + twins + orchestrator all in
one worker, one Render service, one bill). This is optional — if
you're pointing at a real data center instead of the simulator, or
running the simulator as its own separate service, leave this unset.

No absolute paths, no local-machine assumptions (section 12.2).
"""
import os
import threading

from twins.rack_twin import RackTwin
from twins.cooling_twin import CoolingTwin
from twins.occupancy_twin import OccupancyTwin
from twins.energy_twin import EnergyTwin
from orchestrator.orchestrator import Orchestrator

RACK_IDS = ["SR-RACK-01", "SR-RACK-02", "SR-RACK-03"]


def _env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def main():
    broker_host = os.environ["MQTT_HOST"]

    if _env_bool("RUN_SIMULATOR"):
        # Imported here, not at module top, so a deploy that doesn't
        # set RUN_SIMULATOR never even imports the simulator-side
        # client setup — keeps the two concerns decoupled.
        from sensor_simulator import run_simulator

        broker_port = int(os.environ.get("MQTT_PORT", 8883))
        use_tls = os.environ.get("MQTT_TLS", "true").lower() != "false"
        sim_thread = threading.Thread(
            target=run_simulator,
            kwargs=dict(
                host=broker_host,
                port=broker_port,
                use_tls=use_tls,
                username=os.environ.get("MQTT_USERNAME"),
                password=os.environ.get("MQTT_PASSWORD"),
                auto_anomaly=_env_bool("AUTO_ANOMALY"),
            ),
            daemon=True,
        )
        sim_thread.start()

    twins = [CoolingTwin(broker_host=broker_host), OccupancyTwin(broker_host=broker_host),
             EnergyTwin(broker_host=broker_host)]
    twins += [RackTwin(rack_id, broker_host=broker_host) for rack_id in RACK_IDS]

    threads = [threading.Thread(target=t.run_forever, daemon=True) for t in twins]
    for t in threads:
        t.start()

    orch = Orchestrator(broker_host=broker_host)
    orch.run_forever()  # blocks — keep in main thread


if __name__ == "__main__":
    main()
