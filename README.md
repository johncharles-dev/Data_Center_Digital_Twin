# S2 — Prediction and orchestration

Scope per plan section 11.1: model training and evaluation, twin state
objects, orchestrator, recommendation engine, inference service.

**No HTML anywhere in this bundle — the dashboard is S3's deliverable,
not S2's, and is intentionally excluded.** `sensor_simulator.py` is
included only because S2's twins/orchestrator need something publishing
telemetry to actually run and be tested against — it's S1's file, not
S2's, but S2 can't demo without it.

## Layout

```
twins/            Four sub-twins (rack x3, cooling, occupancy, energy)
orchestrator/      Fuses twin state, applies rules, publishes recommendation
inference/         Model loader (trend-baseline fallback -> trained model)
dataset/           Batch training-data generator (140 labeled CRAC-01 runs)
notebooks/         train_crac_model.ipynb — already executed, results inline
main.py            Entry point — starts all twins + orchestrator
                    (RUN_SIMULATOR=true also starts sensor_simulator.py
                    in the same process — see env vars below)
sensor_simulator.py S1's telemetry source — needed to run/test S2 locally
watch.py           Cross-platform MQTT topic watcher — use instead of
                    mosquitto_sub, no separate install needed
render.yaml, Procfile, Dockerfile   Deploy config (any one works)
```

## Verify it's working

```bash
python watch.py "datacenter/predictions/CRAC-01"
python watch.py "datacenter/status/#"     # backend liveness check
```

## Reliability (added after initial build)

- **Connect retry**: all MQTT clients retry on startup connect failure
  (12 attempts, 5s apart) instead of crashing on a broker cold-start race
- **Auto-reconnect**: mid-run disconnects trigger paho's reconnect
  logic automatically
- **Last-Will-and-Testament**: every twin + the orchestrator publishes
  `datacenter/status/<component>` = `"offline"` (retained), which the
  BROKER auto-publishes if the process dies ungracefully — verified
  with a real `SIGKILL` test, no cooperation from the dying process
  needed. `"online"` is published back once (re)connected.

## Train the model

```bash
pip install -r requirements.txt --break-system-packages
jupyter notebook notebooks/train_crac_model.ipynb
```
Produces `models/crac_failure_model.joblib` — `inference/model_loader.py`
picks it up automatically once it exists, building its feature vector
BY NAME from the model's own saved `feature_cols` (not a second
hardcoded list — this is what a 7-vs-11 feature mismatch bug taught us
to avoid). Until the model file exists, the orchestrator runs on a
trend-baseline fallback so it's never blocked on training being finished.

## Run against a broker

```bash
export MQTT_HOST=<broker-host>
export MQTT_PORT=1883       # 8883 for HiveMQ Cloud + MQTT_TLS=true
export MQTT_TLS=false        # true for any cloud broker
export MQTT_USERNAME=...     # only needed if the broker requires auth
export MQTT_PASSWORD=...
export RUN_SIMULATOR=true    # also runs sensor_simulator.py in this process
export AUTO_ANOMALY=false    # true = auto-trigger CRAC-01 fault ~20s after start
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
5. Reliability (retry/LWT) added once the happy path was verified working
