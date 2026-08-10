"""Attempts what the ACL should forbid, and records what the broker says.

A config file is a claim. This is the evidence: eight attempts against the
running broker, each reporting the actual protocol response and the matching
broker log line — not a re-reading of mosquitto/acl.

MQTT v5 is used deliberately. Under 3.1.1 an ACL-denied publish is dropped
silently: the client gets a normal PUBACK and cannot tell it was refused, so
a "denied" result would be indistinguishable from "delivered". v5 carries a
reason code on the PUBACK, so the broker states its refusal explicitly.

    ./run.sh                              # broker must be up
    python3 tests/test_broker_security.py

Exit status is 0 only if every case behaved as the policy requires.
"""
import json
import os
import subprocess
import sys
import time

import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion
import paho.mqtt.properties  # noqa: F401  (v5 support)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HOST = os.environ.get("MQTT_HOST", "localhost")
PORT = int(os.environ.get("MQTT_PORT", 1883))
CREDS_PATH = os.path.join(os.path.dirname(__file__), "..", "mosquitto", "credentials.json")
CONTAINER = "dcdt-broker"

results = []


def code(rc):
    """Numeric value of a v5 ReasonCode (or a plain int, or nothing).

    paho returns a ReasonCode object on v5 and a bare int on 3.1.1, and
    ReasonCode is not int()-convertible — hence this rather than int(rc).
    -1 means the broker never answered at all, which is itself a result.
    """
    if rc is None:
        return -1
    return getattr(rc, "value", rc)


def name(rc):
    """Human-readable name for a reason code, e.g. 'Not authorized'."""
    if rc is None:
        return "no response from broker"
    getter = getattr(rc, "getName", None)
    return getter() if getter else str(rc)


def creds():
    with open(CREDS_PATH) as fh:
        return json.load(fh)


class Client:
    """A v5 client that records the reason codes the broker returns."""

    def __init__(self, username=None, password=None, client_id="sectest"):
        self.connack = None
        self.suback = None
        self.puback = None
        self.received = []
        self.c = mqtt.Client(CallbackAPIVersion.VERSION2,
                             client_id=client_id, protocol=mqtt.MQTTv5)
        if username is not None:
            self.c.username_pw_set(username, password)
        self.c.on_connect = self._on_connect
        self.c.on_subscribe = self._on_subscribe
        self.c.on_publish = self._on_publish
        self.c.on_message = self._on_message

    def _on_connect(self, c, u, flags, rc, props=None):
        self.connack = rc

    def _on_subscribe(self, c, u, mid, rcs, props=None):
        self.suback = rcs[0] if rcs else None

    def _on_publish(self, c, u, mid, rc=None, props=None):
        self.puback = rc

    def _on_message(self, c, u, msg):
        self.received.append((msg.topic, msg.payload))

    def connect(self, timeout=5.0):
        self.c.connect(HOST, PORT, keepalive=10)
        self.c.loop_start()
        deadline = time.time() + timeout
        while self.connack is None and time.time() < deadline:
            time.sleep(0.05)
        return self.connack

    def subscribe(self, topic, timeout=5.0):
        self.c.subscribe(topic, qos=1)
        deadline = time.time() + timeout
        while self.suback is None and time.time() < deadline:
            time.sleep(0.05)
        return self.suback

    def publish(self, topic, payload, timeout=5.0):
        info = self.c.publish(topic, payload, qos=1)
        deadline = time.time() + timeout
        while self.puback is None and time.time() < deadline:
            time.sleep(0.05)
        try:
            info.wait_for_publish(timeout=1)
        except (ValueError, RuntimeError):
            pass
        return self.puback

    def close(self):
        try:
            self.c.loop_stop()
            self.c.disconnect()
        except Exception:
            pass


def broker_log_since(marker_time, pattern):
    """Returns broker log lines matching `pattern` since the test started."""
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", marker_time, CONTAINER],
            capture_output=True, text=True, timeout=15,
        )
        lines = (out.stdout + out.stderr).splitlines()
        return [l.strip() for l in lines if pattern.lower() in l.lower()]
    except Exception as e:
        return [f"(could not read broker log: {e})"]


def record(n, attempt, expected, observed, ok, log_lines=()):
    results.append(dict(n=n, attempt=attempt, expected=expected,
                        observed=observed, ok=ok, log=list(log_lines)))
    status = "PASS" if ok else "FAIL"
    print(f"\n[{n}] {attempt}")
    print(f"     expected : {expected}")
    print(f"     OBSERVED : {observed}   -> {status}")
    for l in log_lines[:2]:
        print(f"     broker   : {l}")


def main():
    c = creds()
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 2))

    print("=" * 74)
    print("BROKER SECURITY — attempting what the ACL forbids")
    print(f"broker {HOST}:{PORT}, MQTT v5, container {CONTAINER}")
    print("=" * 74)

    # 1 -------------------------------------------------------------------
    cl = Client(client_id="anon-probe")
    rc = cl.connect()
    cl.close()
    record(1, "Connect with no credentials at all",
           "CONNACK 135 Not authorized",
           f"CONNACK {code(rc)} {name(rc)}",
           code(rc) == 135,
           broker_log_since(started, "denied") or broker_log_since(started, "anon-probe"))

    # 2 -------------------------------------------------------------------
    cl = Client("viewer", "definitely-not-the-password", client_id="badpw-probe")
    rc = cl.connect()
    cl.close()
    record(2, "Connect as viewer with a wrong password",
           "CONNACK 135 Not authorized",
           f"CONNACK {code(rc)} {name(rc)}",
           code(rc) == 135)

    # 6 (run early: this subscriber witnesses cases 3 and 8) ---------------
    watcher = Client("viewer", c["viewer"], client_id="viewer-watch")
    wrc = watcher.connect()
    wsub = watcher.subscribe("datacenter/recommendations/+")
    record(6, "viewer subscribes to a topic its ACL permits",
           "CONNACK 0, SUBACK granted (qos 1)",
           f"CONNACK {code(wrc)}, SUBACK {code(wsub)}",
           code(wrc) == 0 and code(wsub) <= 2)

    # 3 -------------------------------------------------------------------
    t3 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    cl = Client("twin-cooling", c["twin-cooling"], client_id="twin-cooling")
    cl.connect()
    before = len(watcher.received)
    rc = cl.publish("datacenter/recommendations/room",
                    json.dumps({"action": "FORGED", "source": "twin-cooling"}))
    time.sleep(1.0)
    delivered = len(watcher.received) > before
    cl.close()
    record(3, "twin-cooling publishes a forged RECOMMENDATION",
           "PUBACK 135 Not authorized, and no delivery to subscribers",
           f"PUBACK {code(rc)} {name(rc)}; "
           f"delivered to viewer: {delivered}",
           code(rc) == 135 and not delivered,
           broker_log_since(t3, "denied"))

    # 4 -------------------------------------------------------------------
    t4 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    cl = Client("twin-cooling", c["twin-cooling"], client_id="twin-cooling-b")
    cl.connect()
    rc = cl.publish("datacenter/twin-state/energy",
                    json.dumps({"forged": True}))
    cl.close()
    record(4, "twin-cooling forges ANOTHER twin's state",
           "PUBACK 135 Not authorized",
           f"PUBACK {code(rc)} {name(rc)}",
           code(rc) == 135,
           broker_log_since(t4, "denied"))

    # 5 -------------------------------------------------------------------
    t5 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    cl = Client("viewer", c["viewer"], client_id="viewer-write")
    cl.connect()
    rc = cl.publish("datacenter/twin-state/cooling", json.dumps({"forged": True}))
    cl.close()
    record(5, "viewer (the public dashboard credential) tries to publish",
           "PUBACK 135 Not authorized — the embedded credential is read-only",
           f"PUBACK {code(rc)} {name(rc)}",
           code(rc) == 135,
           broker_log_since(t5, "denied"))

    # 7 -------------------------------------------------------------------
    # Mosquitto does NOT refuse a subscription its ACL disallows — it grants
    # the SUBACK and then never delivers. Measured, not assumed: an earlier
    # version of this case expected SUBACK 0x80 and observed "Granted QoS 1".
    # So the property worth asserting is delivery, not the SUBACK: subscribe
    # as an identity with no read access, have the orchestrator publish a
    # real recommendation, and confirm nothing arrives while the legitimately
    # subscribed viewer receives it.
    t7 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 1))
    eavesdropper = Client("sensor-simulator", c["sensor-simulator"], client_id="sim-sub")
    eavesdropper.connect()
    sub = eavesdropper.subscribe("datacenter/recommendations/room")

    publisher = Client("orchestrator", c["orchestrator"], client_id="orch-probe-7")
    publisher.connect()
    watcher_before = len(watcher.received)
    publisher.publish("datacenter/recommendations/room",
                      json.dumps({"action": "probe", "source": "orchestrator"}))
    time.sleep(1.5)

    eavesdropped = len(eavesdropper.received)
    witnessed = len(watcher.received) > watcher_before
    eavesdropper.close()
    publisher.close()

    record(7, "sensor-simulator subscribes to recommendations (no read in its ACL)",
           "no messages delivered, even though the SUBACK may be granted",
           f"SUBACK {code(sub)} {name(sub)}; messages received by "
           f"sensor-simulator: {eavesdropped}; same message received by "
           f"viewer: {witnessed}",
           eavesdropped == 0 and witnessed,
           broker_log_since(t7, "denied"))

    # 8 -------------------------------------------------------------------
    cl = Client("orchestrator", c["orchestrator"], client_id="orch-probe")
    cl.connect()
    before = len(watcher.received)
    rc = cl.publish("datacenter/recommendations/room",
                    json.dumps({"action": "legitimate", "source": "orchestrator"}))
    time.sleep(1.0)
    delivered = len(watcher.received) > before
    cl.close()
    record(8, "orchestrator publishes a recommendation (the ONE identity allowed)",
           "PUBACK 0 Success, and delivered to subscribers",
           f"PUBACK {code(rc)} {name(rc)}; "
           f"delivered to viewer: {delivered}",
           code(rc) == 0 and delivered)

    watcher.close()

    # ---------------------------------------------------------------- summary
    print("\n" + "=" * 74)
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  [{r['n']}] {r['attempt']}")
    print(f"\n{passed}/{len(results)} cases behaved as the policy requires.")
    print("=" * 74)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
