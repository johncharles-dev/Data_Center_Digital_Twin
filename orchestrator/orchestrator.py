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

# Probability at or above which the model's opinion is actionable.
ACTION_THRESHOLD = 0.5
# Mean per-rack compute utilisation above which the room is "busy".
LOAD_DRIVEN_FACTOR = 0.8


def is_load_driven(load_factor, cooling_state):
    """True when the room is hot because it is BUSY, not because the CRAC
    is failing.

    The original form of this predicate was

        load_factor > 0.8 and failure_probability < 0.3

    but it was only ever evaluated inside a branch already gated on
    failure_probability >= 0.5, so the second conjunct was always False
    and the suppression could never fire — occupancy data had no effect
    on the outcome even once it existed. tests/test_load_driven_branch.py
    sweeps the reachable input space and asserts that.

    The replacement keeps the load gate and swaps the self-contradictory
    probability gate for the signal that actually distinguishes the two
    causes: an equipment-specific sensor trip. High utilisation with no
    trip is load; any trip is the equipment itself, whatever the load.
    """
    if cooling_state.get("threshold_flags"):
        return False
    return load_factor > LOAD_DRIVEN_FACTOR


def should_recommend(failure_probability, load_factor, cooling_state):
    """Whether to publish a recommendation for this prediction."""
    if failure_probability < ACTION_THRESHOLD:
        return False
    return not is_load_driven(load_factor, cooling_state)


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

        # Re-run the model only when the COOLING state changes.
        #
        # model.predict() is a pure function of the cooling twin's state,
        # but this used to fire on every twin-state message — six twins,
        # and the energy twin republishes on all four of its inputs, so
        # roughly ten full RandomForest + GradientBoosting inferences per
        # simulated sample instead of one. At demo acceleration that is
        # enough to fall behind the broker: a 68s run at 600x delivered
        # ~13,500 twin-state messages and the orchestrator got through
        # 128 of them, so it never saw the degraded end of the run at all.
        #
        # The other twins' state is still recorded above and is read at
        # evaluation time; it just no longer triggers an inference of its
        # own. Cooling arrives every sample, so nothing is delayed.
        if twin_id == "cooling":
            self._evaluate()

    def _evaluate(self):
        cooling = self.twin_state.get("cooling", {})
        if not cooling:
            return  # nothing to predict on yet

        prediction = self.model.predict(cooling)
        self._publish_prediction(prediction)

        # distinguish load-driven heat from genuine fault
        load_factor = self.twin_state.get("occupancy", {}).get("load_factor", 0)

        if should_recommend(prediction["failure_probability"], load_factor, cooling):
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
