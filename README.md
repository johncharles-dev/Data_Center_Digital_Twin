# Intelligent Data Centre Digital Twin

**Project 2 — Intelligent Ecosystem & Strategic Optimization (Advanced)**

Predictive cooling maintenance for a three-rack server room. Six twins of four
types — three rack twins plus cooling, occupancy and energy — feed a central
orchestrator with live MQTT telemetry from three racks and one CRAC unit; a
trained classifier reads eleven CRAC signals, including two ten-minute trend
features, and predicts equipment failure up to four hours ahead. The system
names the likely mechanism from the sensor limits that have tripped, recommends a
proportionate action with its estimated cost, and records every decision in a
hash-chained audit trail. Nothing actuates — every output is advisory.

## Live demo

**<https://5090-server.tail391a63.ts.net/>**

The dashboard runs against live telemetry, not a recording. The hosted instance
plays at 75× real time, so a degradation run completes about every two minutes
and the interesting window — model warning while every sensor threshold is still
silent — comes round shortly after you open it.

## Quick start

From a clean clone. Needs Docker and Python 3.11+.

```bash
git clone https://github.com/johncharles-dev/Data_Center_Digital_Twin.git
cd Data_Center_Digital_Twin
pip install -r requirements.txt     # add --break-system-packages on Debian/Ubuntu
./run.sh                            # terminal 1 — the system itself
```

Then, in a second terminal, to see it:

```bash
./serve_demo.sh --local             # terminal 2 — dashboard on loopback
```

Open **<http://127.0.0.1:8011/>**.

`run.sh` checks Docker and the Python packages first and names the fix if either
is missing, then bootstraps everything a fresh clone lacks: broker credentials,
TLS certificates and per-service passwords (all deliberately absent from git, so
a clone cannot connect to anyone else's broker). It starts Mosquitto in Docker,
waits for it to accept connections, then launches the twins, orchestrator,
telemetry simulator, occupancy publisher and audit sink. Nothing is interactive
and nothing needs sudo.

`serve_demo.sh --local` writes `dashboard/config.js` — the read-only viewer
credential the page needs, which is gitignored and therefore absent from a fresh
clone — and serves `dashboard/` on loopback. No Tailscale, no public exposure.
The trained model is committed, so there is nothing to train.

```bash
./run.sh --interval 0.4   # slower playback; the hosted demo runs at this speed
./run.sh --once           # one degradation cycle instead of looping
./run.sh --stop           # stop the broker
```

The default is `--interval 0.05`, which cycles in roughly fifteen seconds; the
hosted demo runs at `0.4`, roughly two minutes, which is easier to watch.
Playback speed changes only how often telemetry is published — each tick advances
the simulated clock by at most 30 s, the interval the model was trained at, so
the trend features stay in range at any speed (`tests/test_substep_cap.py`).

## Repository layout

| Path | What it holds |
|---|---|
| `main.py` | Entry point — runs the six twins and the orchestrator in one process |
| `sensor_simulator.py` | Telemetry source for three racks and CRAC-01, carried forward from Project 1 |
| `occupancy_publisher.py` | Staff presence and compute load, feeding the occupancy twin |
| `acknowledge.py` | Operator acknowledgement of a recommendation |
| `watch.py` | Cross-platform MQTT topic watcher; no broker client to install |
| `twins/` | The four twin types — rack (×3), cooling, occupancy, energy |
| `orchestrator/` | Fuses twin state, applies the recommendation rules, publishes decisions |
| `inference/` | Model loader; builds its feature vector by name from the saved model |
| `dataset/` | Reproducible training-data generator, 140 labelled CRAC-01 runs |
| `models/` | The trained classifier and time-to-failure regressor |
| `notebooks/` | Model training, committed with outputs |
| `audit/` | Append-only, hash-chained decision trail |
| `dashboard/` | Read-only operator page, vanilla JS with vendored mqtt.js |
| `mosquitto/` | Broker config, per-identity topic ACL, credential generator |
| `tests/` | Verification suite — see below |
| `docs/` | Report, diagram, pitch, governance document |

## Deliverables

| Deliverable | Path |
|---|---|
| Predictive model output | `notebooks/train_crac_model.ipynb` |
| Integrated ecosystem diagram | `docs/ecosystem_diagram.png` · `docs/ecosystem_diagram.svg` |
| Executive pitch | `docs/Executive_Pitch.pptx` |
| Report | `docs/P2_Intelligent_Ecosystem_Report.docx` · `.md` |
| Governance and roadmap | `docs/Governance_Ethics_Roadmap.docx` |

The report cites a file and line for every claim it makes, and a command for every
number.

## Verifying it works

With the stack running:

```bash
python3 tests/test_broker_security.py     # 8 access-control attempts against the live broker
python3 tests/test_audit_chain.py         # 17 assertions: tampering and dropped decisions are detectable
python3 tests/test_energy_model.py        # 18 assertions: CRAC power model and PUE bounds
python3 tests/test_slope_units.py         # live trend features match the units used in training
python3 tests/test_substep_cap.py         # simulated clock is capped, so playback speed cannot skew features
python3 tests/test_load_driven_branch.py  # a busy room is distinguishable from a failing one
```

All six pass. To check a real run end to end, capture some traffic and analyse it
— Ctrl+C the capture after a couple of minutes:

```bash
VIEWER=$(python3 -c "import json;print(json.load(open('mosquitto/credentials.json'))['viewer'])")
python3 watch.py '#' --user viewer --pass "$VIEWER" > capture.txt
python3 tests/analyse_live_run.py capture.txt
```

That asserts the four properties the project rests on: trend features inside the
training distribution, the model firing ahead of the threshold rules rather than
re-deriving them, no time-to-failure countdown published below the action
threshold, and occupancy genuinely reaching the decision.

## Deployment

**Nothing here is needed to reproduce the project** — Quick start above is the
whole reproduction path. This section describes only how the public demo happens
to be hosted.

The live demo runs from `run.sh` on a single machine, supervised by two systemd
user services, with the dashboard and the broker's WebSocket listener published
over Tailscale Funnel by `./serve_demo.sh` with no arguments — the page on 443,
the broker on 8443. Tailscale is how that one machine reaches the internet; it is
not a dependency of the system. Run without it and `serve_demo.sh` exits saying
so, which is why the reproduction path uses `--local` instead.

`render.yaml`, `Dockerfile` and `Procfile` are a separate, unused cloud path for
the consumer side only: they run `main.py` as an always-on worker against an
external broker. They are not how the demo runs, and `render.yaml` does not start
`occupancy_publisher.py`, so a cloud deployment would lose the load-versus-fault
distinction.

## Configuration

The broker refuses anonymous connections. Every component authenticates as itself
against a least-privilege topic ACL (`mosquitto/acl`), because `main.py` runs six
twins and the orchestrator in one process — one shared credential there would hold
the union of all seven permissions. `run.sh` exports each service's credential
from `mosquitto/credentials.json`, so a local run needs nothing set by hand.

To point the consumer side at an external broker — the `render.yaml` path:

| Variable | Default | Purpose |
|---|---|---|
| `MQTT_HOST` | *required* | Broker hostname |
| `MQTT_PORT` | `8883` | Use 1883 without TLS |
| `MQTT_TLS` | `true` | `false` for a plain local broker |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | Shared fallback credential |
| `MQTT_USERNAME_<SERVICE>` / `MQTT_PASSWORD_<SERVICE>` | — | Per-service credential, preferred — e.g. `MQTT_USERNAME_TWIN_COOLING` |
| `RUN_SIMULATOR` | `false` | Also run the telemetry simulator in this process |
| `AUTO_ANOMALY` | `false` | Trigger a CRAC-01 fault shortly after start |

A missing or wrong credential is reported rather than swallowed. MQTT returns the
refusal in CONNACK — after the socket has opened — so a client that ignores the
result code goes on to subscribe and publish into a connection the broker has
already closed: it emits nothing, raises nothing, and looks healthy. Both client
paths check the code and name the variable to set (`twins/base_twin.py:95`,
`orchestrator/orchestrator.py:110`).

Liveness is observable rather than assumed. Every twin and the orchestrator
publishes a retained `datacenter/status/<component>` marker, which the broker
flips to `offline` by Last-Will-and-Testament if the process dies without saying
goodbye:

```bash
python3 watch.py 'datacenter/status/#'   # all seven components, online/offline
```

## Limitations

- **The telemetry is simulated.** No figure in this repository has been measured
  against a physical CRAC unit.
- **One fault profile is modelled** — bearing wear coupled to filter loading. A
  third mechanism sits outside the training distribution, where the model reports
  a low probability rather than an error.
- **The demo depends on one machine.** If it is off, the URL is down; the code and
  every result remain reproducible from a clone.
- **The dashboard's broker credential is public by design.** It is read-only by
  ACL and can write nothing, which is what makes it safe to ship in a page anyone
  can view-source.

Each is stated with its evidence in the relevant section of
`docs/P2_Intelligent_Ecosystem_Report.docx`, alongside the limitations of the
energy model and the ROI assumptions.
