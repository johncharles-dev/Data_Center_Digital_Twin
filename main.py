"""Single entry point — starts all twins and the orchestrator in one
process using threads. Suitable for the always-on Render/Railway/Fly.io
instance described in section 12.1.

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


def main():
    broker_host = os.environ["MQTT_HOST"]

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
