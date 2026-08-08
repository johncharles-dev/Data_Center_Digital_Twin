"""Shared base class for all sub-twins.

Each twin subscribes to raw or upstream MQTT topics, maintains a
processed state dict, and publishes that state to
datacenter/twin-state/<TWIN_ID> on every update.
"""
import json
import os
import time
import paho.mqtt.client as mqtt


class BaseTwin:
    twin_id: str = "base"

    def __init__(self, broker_host, broker_port=None, use_tls=None):
        # Defaults assume the production broker (HiveMQ Cloud, TLS, 8883).
        # Override via env vars for local dev against mosquitto.conf
        # (plaintext, 1883) — see README "Run locally" section.
        if broker_port is None:
            broker_port = int(os.environ.get("MQTT_PORT", 8883))
        if use_tls is None:
            use_tls = os.environ.get("MQTT_TLS", "true").lower() != "false"

        self.state = {}
        self.client = mqtt.Client(client_id=f"twin-{self.twin_id}")
        if use_tls:
            self.client.tls_set()
        mqtt_user = os.environ.get("MQTT_USERNAME")
        mqtt_pass = os.environ.get("MQTT_PASSWORD")
        if mqtt_user:
            self.client.username_pw_set(mqtt_user, mqtt_pass)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(broker_host, broker_port)

    def subscriptions(self):
        """Override: return list of topics this twin listens to."""
        return []

    def handle_message(self, topic, payload):
        """Override: update self.state from an incoming message."""
        raise NotImplementedError

    def _on_connect(self, client, userdata, flags, rc):
        for topic in self.subscriptions():
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError:
            return
        self.handle_message(msg.topic, payload)
        self.publish_state()

    def publish_state(self):
        topic = f"datacenter/twin-state/{self.twin_id}"
        self.client.publish(topic, json.dumps(self.state), qos=1, retain=True)

    def run_forever(self):
        self.client.loop_forever()
