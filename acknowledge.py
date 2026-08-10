"""Acknowledge the current recommendation.

The third audited event type. A recommendation is only half of a decision
record — the other half is whether a human saw it and what they did about it.
This publishes that acknowledgement to datacenter/acks/room, where
audit/audit_sink.py records it alongside the prediction and recommendation it
refers to.

Connects as `operator`, an identity the ACL allows to acknowledge and
explicitly does NOT allow to publish recommendations (mosquitto/acl). The
person acting on a decision cannot author one.

Usage:
    python3 acknowledge.py --action acknowledged --note "filter swapped"
    python3 acknowledge.py --action rejected --note "false alarm, load spike"
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from mqtt_identity import apply_credentials

ACK_TOPIC = "datacenter/acks/room"
# Producer-side sequence counter. Persisted because this is a one-shot CLI:
# an in-memory counter would restart at 1 on every invocation, making the
# audit trail's gap detection meaningless for this source.
SEQ_PATH = os.path.join(os.path.dirname(__file__), "logs", ".ack_seq")


def next_seq():
    os.makedirs(os.path.dirname(SEQ_PATH), exist_ok=True)
    try:
        with open(SEQ_PATH) as fh:
            n = int(fh.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        n = 0
    n += 1
    with open(SEQ_PATH, "w") as fh:
        fh.write(str(n))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--action", default="acknowledged",
                    choices=["acknowledged", "rejected", "deferred"])
    ap.add_argument("--note", default="", help="free-text operator note")
    ap.add_argument("--operator", default=os.environ.get("USER", "unknown"),
                    help="who is acknowledging")
    args = ap.parse_args()

    payload = {
        "source": "operator",
        "seq": next_seq(),
        "action": args.action,
        "note": args.note,
        "operator": args.operator,
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    client = mqtt.Client(client_id="operator")
    if os.environ.get("MQTT_TLS", "false").lower() == "true":
        client.tls_set()
    apply_credentials(client, "operator")

    result = {}

    def on_connect(cl, userdata, flags, rc):
        result["rc"] = rc

    client.on_connect = on_connect
    client.connect(args.host, args.port, keepalive=10)
    client.loop_start()

    for _ in range(50):
        if "rc" in result:
            break
        time.sleep(0.1)

    if result.get("rc") != 0:
        print(f"broker refused the connection: rc={result.get('rc')} "
              f"({mqtt.connack_string(result['rc']) if 'rc' in result else 'no CONNACK'})")
        client.loop_stop()
        return 1

    info = client.publish(ACK_TOPIC, json.dumps(payload), qos=1)
    info.wait_for_publish(timeout=5)
    client.loop_stop()
    client.disconnect()

    print(f"acknowledged: seq={payload['seq']} action={args.action} "
          f"operator={args.operator}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
