import sys, paho.mqtt.client as mqtt

topic = sys.argv[1] if len(sys.argv) > 1 else "datacenter/predictions/CRAC-01"

def on_message(client, userdata, msg):
    print(f"{msg.topic}  {msg.payload.decode()}")

c = mqtt.Client()
c.on_message = on_message
c.connect("localhost", 1883)
c.subscribe(topic)
print(f"Watching {topic} ... Ctrl+C to stop")
c.loop_forever()