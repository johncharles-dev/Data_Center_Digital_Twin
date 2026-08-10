"""Shared base class for all sub-twins.

Each twin subscribes to raw or upstream MQTT topics, maintains a
processed state dict, and publishes that state to
datacenter/twin-state/<TWIN_ID> on every update.

Also publishes liveness to datacenter/status/<TWIN_ID>:
  - "online" (retained) once connected
  - "offline" (retained) automatically via MQTT's Last-Will-and-Testament
    if this process dies ungracefully — the broker publishes the LWT
    itself, so this works even on a hard crash or lost network, not
    just a clean shutdown. This is what a dashboard/monitor should
    subscribe to in order to show "backend unreachable" instead of
    just going silently stale (section 12.3's disconnect test only
    checks the dashboard "doesn't blank" — this is what makes a
    disconnect visibly distinguishable from "nothing new happened").
"""
import json
import os
import time
import paho.mqtt.client as mqtt

CONNECT_RETRY_DELAY_S = 5
CONNECT_MAX_RETRIES = 12  # ~1 minute of retrying before giving up


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
        self.status_topic = f"datacenter/status/{self.twin_id}"

        self.client = mqtt.Client(client_id=f"twin-{self.twin_id}")
        if use_tls:
            self.client.tls_set()
        mqtt_user = os.environ.get("MQTT_USERNAME")
        mqtt_pass = os.environ.get("MQTT_PASSWORD")
        if mqtt_user:
            self.client.username_pw_set(mqtt_user, mqtt_pass)

        # Last-Will-and-Testament: broker auto-publishes this if the
        # connection drops without a clean disconnect (crash, network
        # loss, OOM-kill). Retained so a dashboard connecting late still
        # sees the last known status immediately.
        self.client.will_set(self.status_topic, payload="offline", qos=1, retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        # paho auto-reconnects on a dropped connection once loop_forever()
        # is running, but the INITIAL connect() is a single attempt with
        # no retry — a broker that's momentarily unreachable at startup
        # (cold-start race, redeploy blip) would otherwise crash the
        # whole process instead of waiting it out.
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._connect_with_retry(broker_host, broker_port)

    def _connect_with_retry(self, host, port):
        for attempt in range(1, CONNECT_MAX_RETRIES + 1):
            try:
                self.client.connect(host, port)
                return
            except (ConnectionRefusedError, OSError) as e:
                print(f"[{self.twin_id}] connect attempt {attempt}/{CONNECT_MAX_RETRIES} "
                      f"failed ({e}); retrying in {CONNECT_RETRY_DELAY_S}s")
                time.sleep(CONNECT_RETRY_DELAY_S)
        raise ConnectionError(
            f"[{self.twin_id}] could not connect to {host}:{port} after "
            f"{CONNECT_MAX_RETRIES} attempts"
        )

    def subscriptions(self):
        """Override: return list of topics this twin listens to."""
        return []

    def handle_message(self, topic, payload):
        """Override: update self.state from an incoming message."""
        raise NotImplementedError

    def _on_connect(self, client, userdata, flags, rc):
        for topic in self.subscriptions():
            client.subscribe(topic)
        client.publish(self.status_topic, "online", qos=1, retain=True)

    def _on_disconnect(self, client, userdata, rc):
        # rc != 0 means this was NOT a clean client.disconnect() call —
        # paho will attempt to auto-reconnect (reconnect_delay_set above)
        # and _on_connect will re-publish "online" once it succeeds. The
        # broker's LWT already published "offline" for the gap in between.
        if rc != 0:
            print(f"[{self.twin_id}] unexpected disconnect (rc={rc}), reconnecting...")

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
