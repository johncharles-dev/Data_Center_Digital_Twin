"""Central orchestrator.

Subscribes ONLY to datacenter/twin-state/<TWIN_ID> topics (never raw
telemetry directly — section 3.3's centralized design). Fuses the four
twins' state, runs the CRAC-01 failure model, distinguishes load-driven
heat from equipment fault using occupancy context, compares action
cost via the Energy Twin, and publishes one recommendation.

Also publishes liveness to datacenter/status/orchestrator, same
LWT-backed pattern as the twins (see base_twin.py) — this is the most
important liveness signal to watch, since it's what actually produces
predictions/recommendations; if it dies, the dashboard would otherwise
just show stale numbers with no indication anything's wrong.
"""
import json
import os
import time
import paho.mqtt.client as mqtt

from inference.model_loader import CRACFailureModel
from orchestrator.rules import recommend_action

TWIN_IDS = ["cooling", "occupancy", "energy", "rack-SR-RACK-01", "rack-SR-RACK-02", "rack-SR-RACK-03"]

CONNECT_RETRY_DELAY_S = 5
CONNECT_MAX_RETRIES = 12


class Orchestrator:
    def __init__(self, broker_host, broker_port=None, use_tls=None):
        if broker_port is None:
            broker_port = int(os.environ.get("MQTT_PORT", 8883))
        if use_tls is None:
            use_tls = os.environ.get("MQTT_TLS", "true").lower() != "false"

        self.twin_state = {tid: {} for tid in TWIN_IDS}
        self.model = CRACFailureModel()
        self.status_topic = "datacenter/status/orchestrator"

        self.client = mqtt.Client(client_id="orchestrator")
        if use_tls:
            self.client.tls_set()
        mqtt_user = os.environ.get("MQTT_USERNAME")
        mqtt_pass = os.environ.get("MQTT_PASSWORD")
        if mqtt_user:
            self.client.username_pw_set(mqtt_user, mqtt_pass)

        self.client.will_set(self.status_topic, payload="offline", qos=1, retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._connect_with_retry(broker_host, broker_port)

    def _connect_with_retry(self, host, port):
        for attempt in range(1, CONNECT_MAX_RETRIES + 1):
            try:
                self.client.connect(host, port)
                return
            except (ConnectionRefusedError, OSError) as e:
                print(f"[orchestrator] connect attempt {attempt}/{CONNECT_MAX_RETRIES} "
                      f"failed ({e}); retrying in {CONNECT_RETRY_DELAY_S}s")
                time.sleep(CONNECT_RETRY_DELAY_S)
        raise ConnectionError(
            f"[orchestrator] could not connect to {host}:{port} after "
            f"{CONNECT_MAX_RETRIES} attempts"
        )

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe("datacenter/twin-state/+")
        client.publish(self.status_topic, "online", qos=1, retain=True)

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            print(f"[orchestrator] unexpected disconnect (rc={rc}), reconnecting...")

    def _on_message(self, client, userdata, msg):
        twin_id = msg.topic.split("/")[-1]
        try:
            self.twin_state[twin_id] = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            return
        self._evaluate()

    def _evaluate(self):
        cooling = self.twin_state.get("cooling", {})
        if not cooling:
            return  # nothing to predict on yet

        prediction = self.model.predict(cooling)
        self._publish_prediction(prediction)

        # distinguish load-driven heat from genuine fault
        load_factor = self.twin_state.get("occupancy", {}).get("load_factor", 0)
        is_load_driven = load_factor > 0.8 and prediction["failure_probability"] < 0.3

        if prediction["failure_probability"] >= 0.5 and not is_load_driven:
            action = recommend_action(prediction, cooling, self.twin_state.get("energy", {}))
            self._publish_recommendation(action)

    def _publish_prediction(self, prediction):
        self.client.publish(
            "datacenter/predictions/CRAC-01", json.dumps(prediction), qos=1, retain=True
        )

    def _publish_recommendation(self, action):
        self.client.publish(
            "datacenter/recommendations/room", json.dumps(action), qos=1, retain=True
        )

    def run_forever(self):
        self.client.loop_forever()


if __name__ == "__main__":
    orch = Orchestrator(broker_host=os.environ["MQTT_HOST"])
    orch.run_forever()
