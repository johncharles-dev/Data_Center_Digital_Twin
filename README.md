# S2 — Prediction and orchestration

Scope per plan section 11.1: model training and evaluation, twin state
objects, orchestrator, recommendation engine, inference service.

This is a subset of the full project — it does NOT include the
dashboard (S3), mosquitto.conf, or sensor_simulator.py (S1 telemetry
contract). To run this against live data you need a broker and a
telemetry source publishing to `datacenter/racks/*` and
`datacenter/racks/CRAC-01` in the schema `twins/*_twin.py` expect —
either the S1 simulator or your own.

## Layout

```
twins/            Four sub-twins (rack x3, cooling, occupancy, energy)
orchestrator/      Fuses twin state, applies rules, publishes recommendation
inference/         Model loader (trend-baseline fallback -> trained model)
dataset/           Batch training-data generator (140 labeled CRAC-01 runs)
notebooks/         train_crac_model.ipynb — already executed, results inline
main.py            Entry point — starts all twins + orchestrator
render.yaml, Procfile, Dockerfile   Deploy config (any one works)
```

## Train the model

```bash
pip install -r requirements.txt --break-system-packages
jupyter notebook notebooks/train_crac_model.ipynb
```
Produces `models/crac_failure_model.joblib` — `inference/model_loader.py`
picks it up automatically once it exists; until then, the orchestrator
runs on a trend-baseline fallback so it's never blocked on training
being finished (verified — see build order below).

## Run against a broker

```bash
export MQTT_HOST=<broker-host>
export MQTT_PORT=1883       # 8883 for HiveMQ Cloud + MQTT_TLS=true
export MQTT_TLS=false        # true for any cloud broker
export MQTT_USERNAME=...     # only needed if the broker requires auth
export MQTT_PASSWORD=...
python main.py
```

Verified end-to-end against a real local broker: twins publish
correctly-shaped state to `datacenter/twin-state/*`, orchestrator fuses
it and publishes to `datacenter/predictions/CRAC-01` and
`datacenter/recommendations/room`.

## Deploy (section 12.1)

Any of `render.yaml` (Render Blueprint), `Procfile` (Railway), or
`Dockerfile` (portable across all three, including Fly.io) will run
`python main.py` as an always-on worker — no HTTP port needed, this is
MQTT pub/sub only. Set the same env vars as above on whichever
platform you pick, pointed at your HiveMQ Cloud cluster.

## Build order this followed

1. Trend-baseline fallback shipped first, unblocking wiring before any
   model existed
2. Orchestrator built as pass-through, proving the MQTT plumbing
3. Real model swapped in once trained — one-line change, no rewiring
4. Cross-twin logic (load-vs-fault distinction) added last
