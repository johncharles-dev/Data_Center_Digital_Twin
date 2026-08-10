"""Central orchestrator.

Subscribes ONLY to datacenter/twin-state/<TWIN_ID> topics (never raw
telemetry directly — section 3.3's centralized design). Fuses the four
twins' state, runs the CRAC-01 failure model, distinguishes load-driven
heat from equipment fault using occupancy context, compares action
cost via the Energy Twin, and publishes one recommendation.
"""
import json
import os
import paho.mqtt.client as mqtt

from inference.model_loader import CRACFailureModel
from orchestrator.rules import recommend_action

TWIN_IDS = ["cooling", "occupancy", "energy", "rack-SR-RACK-01", "rack-SR-RACK-02", "rack-SR-RACK-03"]


class Orchestrator:
    def __init__(self, broker_host, broker_port=None, use_tls=None):
        if broker_port is None:
            broker_port = int(os.environ.get("MQTT_PORT", 8883))
        if use_tls is None:
            use_tls = os.environ.get("MQTT_TLS", "true").lower() != "false"

        self.twin_state = {tid: {} for tid in TWIN_IDS}
        self.model = CRACFailureModel()

        self.client = mqtt.Client(client_id="orchestrator")
        if use_tls:
            self.client.tls_set()
        mqtt_user = os.environ.get("MQTT_USERNAME")
        mqtt_pass = os.environ.get("MQTT_PASSWORD")
        if mqtt_user:
            self.client.username_pw_set(mqtt_user, mqtt_pass)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(broker_host, broker_port)

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe("datacenter/twin-state/+")

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
