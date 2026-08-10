"""Cross-platform MQTT watcher — replaces mosquitto_sub, no install
needed (paho-mqtt is already in requirements.txt).

Usage:
  python watch.py                                    # local dev, no auth
  python watch.py "datacenter/status/#"               # different topic
  python watch.py "datacenter/predictions/CRAC-01" --host <hivemq-host> --tls --user <u> --pass <p>
"""
import argparse
import paho.mqtt.client as mqtt


def on_message(client, userdata, msg):
    print(f"{msg.topic}  {msg.payload.decode()}")


def on_connect(client, userdata, flags, rc):
    status = "connected" if rc == 0 else f"connect failed (rc={rc})"
    print(f"[watch] {status}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("topic", nargs="?", default="datacenter/predictions/CRAC-01")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=None)  # defaults below based on --tls
    ap.add_argument("--tls", action="store_true", help="use TLS (needed for HiveMQ Cloud)")
    ap.add_argument("--user", default=None)
    ap.add_argument("--pass", dest="password", default=None)
    args = ap.parse_args()

    port = args.port or (8883 if args.tls else 1883)

    c = mqtt.Client()
    if args.tls:
        c.tls_set()
    if args.user:
        c.username_pw_set(args.user, args.password)

    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(args.host, port)
    c.subscribe(args.topic)

    print(f"[watch] {args.host}:{port} tls={args.tls} topic={args.topic}  (Ctrl+C to stop)")
    c.loop_forever()


if __name__ == "__main__":
    main()
