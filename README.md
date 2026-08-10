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

Three processes make a complete run — `main.py` is only the consumer side:

```bash
python main.py                                    # twins + orchestrator
python sensor_simulator.py --interval 0.05 --anomaly   # racks + CRAC-01
python occupancy_publisher.py --interval 0.05          # staff + workload
```

`occupancy_publisher.py` is what feeds `datacenter/occupancy/staff` and
`/workload`. Without it OccupancyTwin never receives anything,
`load_factor` stays at its default of 0, and the orchestrator cannot
tell load-driven heat from an equipment fault.

### Playback speed vs. the degradation

The fault takes 8 simulated hours and each publish tick advances the
simulated clock by at most 30s — the interval the failure model was
trained at. Watch it faster by publishing more often (`--interval`),
never by making the fault steeper: the model's two trend features are
rates, and steepening the fault pushes them outside the range the model
was fitted on. `tests/test_substep_cap.py` asserts both properties.

## CRAC energy model

`twins/energy_twin.py` computes CRAC electrical draw from telemetry the
system already publishes. Every coefficient below is derived, not fitted —
there is no measured CRAC to fit against, so the alternative to a stated
derivation is a guess, and the ROI case cannot rest on one.

**Fan — affinity law on measured shaft speed**

```
P_fan = P_FAN_RATED × (fan_rpm / 3200)³
```

| Coefficient | Value | Basis |
|---|---|---|
| `FAN_RPM_RATED` | 3200 rpm | nominal duty point, `CRAC_BASE["fan_rpm"]` |
| `NOMINAL_AIRFLOW_M3_S` | 1.6045 m³/s | 3400 CFM nominal, `CRAC_BASE["airflow_cfm"]` |
| `FAN_TOTAL_PRESSURE_PA` | 500 Pa | downflow CRAC into a raised-floor plenum: ~150–250 Pa external static plus coil, filter and plenum losses |
| `FAN_COMBINED_EFFICIENCY` | 0.55 | belt-driven centrifugal, fan × motor × drive |
| **`P_FAN_RATED_KW`** | **1.459 kW** | `Q·Δp/η = 1.6045 × 500 / 0.55 = 1459 W` |

The cube law is why a degrading unit is expensive before it fails: over this
system's own fault profile the fan runs 3200 → 3450 rpm, a **7.8% speed rise
that becomes a 25% power rise**.

**Compressor — linear in reported load fraction**

```
P_comp = P_COMP_RATED × compressor_load_pct / 100
```

| Coefficient | Value | Basis |
|---|---|---|
| `RATED_COOLING_KW` | 30 kW sensible | sized ~1.3× the ~23 kW peak IT load this room reaches |
| `RATED_COP` | 3.0 | mid-range DX CRAC sensible COP at design conditions; high-efficiency units reach 3.5+ |
| **`P_COMP_RATED_KW`** | **10.0 kW** | `30 / 3.0` |

Linear because the modelled unit is a **fixed-speed scroll compressor that
cycles** to meet part load: mean power over a cycle is proportional to
run-time fraction, which is what `compressor_load_pct` reports.

**What it produces**

| State | fan rpm | comp % | fan kW | comp kW | CRAC kW | IT kW | PUE |
|---|---|---|---|---|---|---|---|
| healthy baseline | 3200 | 55 | 1.46 | 5.50 | 6.96 | 18.6 | **1.37** |
| mid-degradation | 3330 | 70 | 1.64 | 7.00 | 8.64 | 21.0 | **1.41** |
| tripped / plateau | 3450 | 85 | 1.83 | 8.50 | 10.33 | 23.4 | **1.44** |

A degraded CRAC draws **3.37 kW more** than a healthy one — SGD 14.55/day at
0.18 SGD/kWh. That difference is the quantity the ROI case is built on.

**Limitations, stated plainly**

- A **variable-speed (inverter) compressor** follows a markedly non-linear
  power curve. This model would misstate such a unit; it describes a cycling
  fixed-speed one.
- COP is **fixed**. A real unit's COP falls as condenser temperature rises,
  so this understates power on hot days.
- An **EC plug fan** would reach ~0.65 combined efficiency, making this
  overstate fan power by roughly 15%.
- Nothing here is validated against a physical CRAC. The coefficients are
  defensible engineering estimates with stated provenance, not measurements.

This replaced `compressor_load_pct * 0.05`, which put CRAC draw at ~2.9 kW
against ~18.6 kW of IT load (PUE 1.16 — not credible for a room cooled this
way) and ignored fan power entirely, so degradation appeared to cost nothing.

## Tests

```bash
python3 tests/test_energy_model.py        # CRAC power model + PUE bounds
python3 tests/test_slope_units.py         # live slopes == training units
python3 tests/test_substep_cap.py         # sub-stepping + speed invariance
python3 tests/test_load_driven_branch.py  # load-vs-fault branch reachable
python3 tests/training_feature_range.py   # training slope distribution
python3 tests/analyse_live_run.py CAPTURE # verify a recorded live run
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
