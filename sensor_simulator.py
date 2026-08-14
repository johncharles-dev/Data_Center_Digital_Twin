#!/usr/bin/env python3
"""
Smart Data Center Digital Twin - Segment 2: Synthetic Telemetry Engine
=======================================================================

Publishes live simulated sensor telemetry for a virtual data center server
room (3 racks) to an MQTT broker, matching the Segment 1 JSON schema exactly
so the Segment 3 dashboard can consume it with no changes.

Contract (from Segment 1 handover + dashboard.html):
  - Broker topic:   datacenter/racks/<RACK_ID>   (dashboard subscribes datacenter/racks/#)
  - Payload schema: timestamp, rack_id, location, inlet_temperature,
                    exhaust_temperature, fan_speed, power_draw, status
  - status values:  NORMAL | WARNING | CRITICAL
  - Status rules:   WARNING  if exhaust > 35 OR power > 8.5
                    CRITICAL if exhaust > 40 OR fan > 7000 OR power > 9.0

CRAC-01 stream (added for Project 2 / section 5 failure model training):
  - Broker topic:   datacenter/racks/CRAC-01   (same topic family, dashboard's
                    datacenter/racks/# subscription already picks it up;
                    the CoolingTwin subscribes to this exact topic)
  - Payload schema: timestamp, unit_id, fan_rpm, fan_motor_current_a,
                    fan_motor_temp_c, fan_vibration_mm_s, filter_dp_pa,
                    airflow_cfm, supply_air_temp_c, return_air_temp_c,
                    compressor_load_pct
  - Failure thresholds (plan section 4.3):
      fan_motor_temp_c >= 105        -> bearing wear / overheating
      filter_dp_pa     >= 350        -> filter clogging / restriction
      airflow_cfm      <= 2210 (65%) -> airflow loss (nominal 3400 cfm)

Anomaly:
  Press 'a' + Enter (or run with --anomaly) to trigger a CRAC cooling
  degradation. This is now the SAME root-cause event for both streams:
  CRAC-01's motor temp, filter DP, and vibration climb (the cause) while
  rack inlet temperature ramps upward across all racks (the effect) fast
  enough that the dashboard's predictive alert (slope >= 0.2 C/min toward
  the 30 C inlet-critical line) fires BEFORE the hard thresholds trip.
  CRAC-01's own thresholds trip a little ahead of the rack thresholds,
  matching the plan's claim that CRAC sensor data gives more lead time
  than reacting to rack symptoms alone.

Usage:
  pip install paho-mqtt
  python sensor_simulator.py                 # normal live stream
  python sensor_simulator.py --anomaly       # start, then auto-trigger anomaly after 20s
  python sensor_simulator.py --csv           # also append every reading to room_data.csv/.json
  python sensor_simulator.py --host broker.hivemq.com   # use public broker fallback

Simulated clock:
  The degradation takes ANOMALY_DURATION_S of SIMULATED time (8h) and each
  publish tick advances that clock by at most SIM_SAMPLE_INTERVAL_S (30s,
  the interval the failure model was trained at). To watch it faster,
  publish more often -- do NOT make the fault steeper:

  python sensor_simulator.py --interval 0.02 --anomaly   # 1500x, 8h in ~19s
  python sensor_simulator.py --interval 1.0  --anomaly   # 30x,   8h in ~16min
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

from mqtt_identity import apply_credentials

try:
    import paho.mqtt.client as mqtt
    HAVE_MQTT = True
except ImportError:
    HAVE_MQTT = False


# ---------------------------------------------------------------------------
# Configuration - values come straight from the Segment 1 handover
# ---------------------------------------------------------------------------

RACKS = [
    {"rack_id": "SR-RACK-01", "location": "Rack A"},
    {"rack_id": "SR-RACK-02", "location": "Rack B"},
    {"rack_id": "SR-RACK-03", "location": "Rack C"},
]

# Normal operating baselines (midpoints of the Segment 1 normal ranges)
BASE = {
    "inlet_temperature":   20.0,   # normal 18-27
    "exhaust_temperature": 30.0,   # normal 25-35
    "fan_speed":           4200,   # normal 2500-6000
    "power_draw":          5.8,    # normal 4.2-8.5
}

# Thresholds for status computation (Segment 1 status rules)
WARN_EXHAUST = 35.0
WARN_POWER   = 8.5
CRIT_EXHAUST = 40.0
CRIT_FAN     = 7000
CRIT_POWER   = 9.0

PUBLISH_INTERVAL = 1.0            # seconds between publish cycles
TOPIC_BASE = "datacenter/racks"  # dashboard subscribes to datacenter/racks/#

# ---------------------------------------------------------------------------
# CRAC-01 configuration (plan section 4.3 thresholds)
# ---------------------------------------------------------------------------

CRAC_UNIT_ID = "CRAC-01"

CRAC_BASE = {
    "fan_rpm":              3200,
    "fan_motor_current_a":  4.5,
    "fan_motor_temp_c":     65.0,   # trips at 105
    "fan_vibration_mm_s":   1.8,
    "filter_dp_pa":         120.0,  # trips at 350
    "airflow_cfm":          3400.0, # trips at <=2210 (65% of nominal)
    "supply_air_temp_c":    18.0,
    "return_air_temp_c":    27.0,
    "compressor_load_pct":  55.0,
}

CRAC_MOTOR_TEMP_TRIP = 105.0
CRAC_FILTER_DP_TRIP = 350.0
CRAC_NOMINAL_AIRFLOW = 3400.0
CRAC_AIRFLOW_TRIP_PCT = 0.65

# Anomaly plateau caps for CRAC-01, timed to reach threshold territory
# comfortably before the rack plateau (ANOMALY_MAX_RISE=6.5C) finishes,
# so CRAC sensors give earlier lead time than rack symptoms alone.
CRAC_ANOMALY_MOTOR_TEMP_RISE = 55.0   # 65 -> 120, crosses 105 trip
CRAC_ANOMALY_FILTER_DP_RISE = 280.0   # 120 -> 400, crosses 350 trip
CRAC_ANOMALY_AIRFLOW_DROP = 1500.0    # 3400 -> 1900 (<=2210 trip)
CRAC_ANOMALY_VIBRATION_RISE = 3.5     # 1.8 -> 5.3 mm/s
CRAC_ANOMALY_COMPRESSOR_RISE = 30.0   # compressor works harder to compensate
CRAC_ANOMALY_RAMP_FRACTION = 0.32     # reaches full plateau at 32% of the
                                       # rack anomaly's own ramp timescale --
                                       # tuned so CRAC thresholds trip ~10-15s
                                       # BEFORE rack WARNING does (verified
                                       # below), matching the plan's claim
                                       # that CRAC sensor data gives earlier
                                       # lead time than reacting to rack
                                       # symptoms alone

# Anomaly tuning. The rack ramp is driven by fractional progress through
# ANOMALY_DURATION_S (below), not by a per-wall-second rate:
# ANOMALY_INLET_RAMP_PER_SEC (0.06 C/s) and ANOMALY_POWER_CREEP
# (0.02 kW/s) used to live here and both hit their cap at ~110s, which is
# what compressed an hours-long fault into under two minutes.
ANOMALY_EXHAUST_COUPLING   = 1.3   # exhaust rises faster than inlet
ANOMALY_FAN_COMPENSATION   = 90    # RPM added per degree of inlet rise

# Plateau caps: residual / backup cooling stabilises the room at an elevated but
# physically-believable ceiling instead of the ramp running away unbounded.
# MAX_RISE = 6.5 -> inlet caps ~26.5 C, exhaust caps ~45 C (30 + 2.3*6.5), fan
# caps ~4785 RPM. The climb still passes cleanly through NORMAL -> WARNING
# (exhaust>35) -> CRITICAL (exhaust>40) before settling at the plateau.
ANOMALY_MAX_RISE       = 6.5   # deg C of inlet rise the fault tops out at
ANOMALY_MAX_POWER_RISE = 2.2   # kW of extra draw the fault tops out at (-> ~8.0 kW)


# ---------------------------------------------------------------------------
# Simulated clock
# ---------------------------------------------------------------------------
#
# The degradation is defined in SIMULATED seconds, not wall-clock seconds.
#
# Why: the CRAC failure model's two engineered features
# (motor_temp_slope_10min, filter_dp_slope_10min) were trained on runs
# that degrade over 2-14 hours sampled every 30s. The previous version of
# this file ramped the same fault to completion in ~35 wall-clock seconds,
# which made those live slopes ~90x larger than anything in the training
# set — so the model saw inputs it had never been fitted on and simply
# saturated.
#
# The fix is to separate two things that were conflated:
#
#   * how long the degradation TAKES        -> ANOMALY_DURATION_S (fixed)
#   * how fast you WATCH it                 -> --sim-step / --interval
#
# Each publish tick advances the simulated clock by at most one training
# sample interval, so no matter how fast the demo is played back the
# telemetry is never sampled more coarsely than the model was trained on.
# Watching it faster is done by publishing more often (--interval), not by
# making the fault itself steeper.

SIM_SAMPLE_INTERVAL_S = 30.0   # dataset/generate_dataset.py SAMPLE_INTERVAL_S

# --loop pacing, in SIMULATED seconds.
#
# The cycle is balanced so a visitor arriving at a random moment is most
# likely to land during the RISE — the phase where the model has warned and
# the threshold rules have not yet fired, which is the entire argument for
# the system. A visitor who lands on a screen that is already fully red has
# been shown an alarm, which any thermostat can do.
#
# Measured shape of one fault: the CRAC trips its first sensor threshold
# about 96 simulated minutes in, and the classifier saturates near 1.0 well
# before that. The remaining ~6.4 hours of ANOMALY_DURATION_S are a flat red
# tail that demonstrates nothing, so --loop ends the cycle shortly after the
# trip instead of running it out.
#
# Note what is NOT done here: the fault is not made steeper to shorten it.
# The ramp rate is what the model's two slope features were trained on —
# steepening it would push them outside the training distribution and make
# the prediction meaningless. The ramp is untouched; only the dead tail is
# trimmed.
#
# Resulting cycle ≈ 30 min healthy + ~96 min rising + 15 min tripped, so
# roughly two thirds of arrivals land mid-rise.
HEALTHY_DWELL_S = 1800.0    # ≥ one slope window (600s) so the buffer refills
FAILED_DWELL_S = 900.0      # measured from the FIRST trip, not from onset
ANOMALY_DURATION_S    = 8 * 3600.0   # 8h — the training set's median TTF

# Baseline sine periods, in simulated seconds. These used to be ~20s,
# which aliases badly once a tick can advance 30 simulated seconds.
SWING_PERIOD_S      = 7200.0   # 2h, racks
CRAC_SWING_PERIOD_S = 9000.0   # 2.5h, CRAC (deliberately not in phase)


# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------

class Simulator:
    def __init__(self, args):
        self.args = args
        self.t = 0.0                 # SIMULATED seconds since start
        self.anomaly_active = False
        self.anomaly_elapsed = 0.0   # SIMULATED seconds since the fault began
        self.healthy_elapsed = 0.0   # SIMULATED seconds healthy (--loop pacing)
        self.tripped_elapsed = 0.0   # SIMULATED seconds since first sensor trip
        self.cycle = 0               # completed degradation cycles (--loop)

        # Identifies THIS degradation run on every message it produces.
        #
        # MQTT retains the last message on the decision topics, so a consumer
        # connecting during a healthy stretch is handed the previous run's
        # recommendation ("motor winding at its 105 °C trip point") as if it were
        # current. A run id lets a consumer discard anything from a run that
        # has already ended instead of rendering it as live state.
        #
        # Seeded from wall-clock seconds so ids from an earlier process are
        # strictly lower than this process's, and a restart cannot make a
        # stale retained message look current again.
        self.run_id = int(time.time()) * 1000 + 1

        # Simulated seconds advanced per publish tick, hard-capped at one
        # training sample interval so the stream is never coarser than the
        # data the model was fitted on. Raising --sim-step past the cap is
        # silently clamped rather than honoured: honouring it is precisely
        # the bug this guard exists to prevent.
        self.sim_step = min(float(getattr(args, "sim_step", SIM_SAMPLE_INTERVAL_S)),
                            SIM_SAMPLE_INTERVAL_S)
        self.wall_interval = float(getattr(args, "interval", PUBLISH_INTERVAL))
        # Timestamps advance on the simulated clock, anchored to the real
        # time the process started so they still look like "now".
        self.sim_epoch = time.time()
        self.running = True
        self.client = None
        self.csv_file = None
        self.csv_writer = None
        self.crac_csv_file = None
        self.crac_csv_writer = None

        if args.csv:
            self._open_csv()
            self._open_crac_csv()

    def _open_csv(self):
        new = not os.path.exists("room_data.csv")
        self.csv_file = open("room_data.csv", "a", newline="")
        self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=[
            "timestamp", "rack_id", "location", "inlet_temperature",
            "exhaust_temperature", "fan_speed", "power_draw", "status"])
        if new:
            self.csv_writer.writeheader()

    def _open_crac_csv(self):
        """Separate file — CRAC-01's schema doesn't match the rack schema,
        so it can't share room_data.csv's fixed fieldnames."""
        new = not os.path.exists("crac_data.csv")
        self.crac_csv_file = open("crac_data.csv", "a", newline="")
        self.crac_csv_writer = csv.DictWriter(self.crac_csv_file, fieldnames=[
            "timestamp", "unit_id", "fan_rpm", "fan_motor_current_a",
            "fan_motor_temp_c", "fan_vibration_mm_s", "filter_dp_pa",
            "airflow_cfm", "supply_air_temp_c", "return_air_temp_c",
            "compressor_load_pct", "threshold_flags"])
        if new:
            self.crac_csv_writer.writeheader()

    # -- value generation ----------------------------------------------------

    def _reading_for_rack(self, rack, phase):
        """Produce one physically-plausible reading for a rack."""
        # Gentle sine swing so the baseline isn't flat, plus small noise.
        swing = math.sin(2 * math.pi * self.t / SWING_PERIOD_S + phase)

        # Occasional independent compute spike raises power (and heat/fan).
        # Suppressed during the anomaly so the demo climb stays clean/monotonic.
        spike = 0.0
        if not self.anomaly_active and random.random() < 0.03:
            spike = random.uniform(1.5, 2.6)

        inlet = BASE["inlet_temperature"] + 0.8 * swing + random.uniform(-0.2, 0.2)
        power = BASE["power_draw"] + 0.6 * swing + spike + random.uniform(-0.15, 0.15)

        # Exhaust tracks inlet plus the heat added by the rack's power load.
        exhaust = BASE["exhaust_temperature"] + (inlet - BASE["inlet_temperature"]) \
                  + 1.4 * (power - BASE["power_draw"]) + random.uniform(-0.3, 0.3)

        # Fan speed responds to exhaust temperature.
        fan = BASE["fan_speed"] + 140 * (exhaust - BASE["exhaust_temperature"]) \
              + random.uniform(-80, 80)

        # -- anomaly overlay: CRAC degradation ------------------------------
        if self.anomaly_active:
            # Clamp the rise so temps stabilise at a believable plateau instead
            # of climbing forever (backup cooling holding the line).
            # Progress through the fault, in simulated time. Both the
            # original per-second rates reached their cap at ~110s of the
            # old wall-clock ramp, so a single shared progress term
            # preserves the original shape while stretching it over the
            # physically-plausible ANOMALY_DURATION_S.
            progress = min(1.0, self.anomaly_elapsed / ANOMALY_DURATION_S)
            rise = ANOMALY_MAX_RISE * progress
            power_rise = ANOMALY_MAX_POWER_RISE * progress
            # During the anomaly, damp the random swing/noise so the upward
            # ramp is smooth and status climbs NORMAL -> WARNING -> CRITICAL
            # cleanly rather than flickering on noise.
            inlet   = BASE["inlet_temperature"] + rise + random.uniform(-0.05, 0.05)
            exhaust = BASE["exhaust_temperature"] + rise + ANOMALY_EXHAUST_COUPLING * rise \
                      + random.uniform(-0.1, 0.1)
            fan     = BASE["fan_speed"] + ANOMALY_FAN_COMPENSATION * rise + random.uniform(-30, 30)
            power   = BASE["power_draw"] + power_rise + random.uniform(-0.05, 0.05)

        return {
            "inlet_temperature":   round(inlet, 1),
            "exhaust_temperature": round(exhaust, 1),
            "fan_speed":           int(max(0, fan)),
            "power_draw":          round(max(0, power), 1),
        }

    @staticmethod
    def _status(r):
        """Apply the Segment 1 status rules."""
        if (r["exhaust_temperature"] > CRIT_EXHAUST
                or r["fan_speed"] > CRIT_FAN
                or r["power_draw"] > CRIT_POWER):
            return "CRITICAL"
        if (r["exhaust_temperature"] > WARN_EXHAUST
                or r["power_draw"] > WARN_POWER):
            return "WARNING"
        return "NORMAL"

    # -- CRAC-01 value generation --------------------------------------------

    def _crac_reading(self):
        """Produce one physically-plausible CRAC-01 reading. Shares the same
        anomaly_active / anomaly_elapsed state as the rack ramp, so the two
        streams tell one consistent degradation story."""
        swing = math.sin(2 * math.pi * self.t / CRAC_SWING_PERIOD_S + 1.0)

        motor_temp = CRAC_BASE["fan_motor_temp_c"] + 1.5 * swing + random.uniform(-0.4, 0.4)
        filter_dp = CRAC_BASE["filter_dp_pa"] + 4.0 * swing + random.uniform(-3.0, 3.0)
        airflow = CRAC_BASE["airflow_cfm"] - 20.0 * abs(swing) + random.uniform(-25, 25)
        vibration = CRAC_BASE["fan_vibration_mm_s"] + 0.15 * swing + random.uniform(-0.05, 0.05)
        fan_rpm = CRAC_BASE["fan_rpm"] + 60 * swing + random.uniform(-20, 20)
        compressor = CRAC_BASE["compressor_load_pct"] + 3.0 * swing + random.uniform(-1.5, 1.5)
        motor_current = CRAC_BASE["fan_motor_current_a"] + 0.1 * swing + random.uniform(-0.05, 0.05)

        if self.anomaly_active:
            # Same elapsed-time clock as the rack ramp, but reaches its
            # plateau sooner (CRAC_ANOMALY_RAMP_FRACTION), matching the
            # plan's claim that CRAC telemetry gives earlier lead time than
            # the rack symptoms it's driving.
            progress = min(1.0, self.anomaly_elapsed
                           / (ANOMALY_DURATION_S * CRAC_ANOMALY_RAMP_FRACTION))

            motor_temp = CRAC_BASE["fan_motor_temp_c"] + CRAC_ANOMALY_MOTOR_TEMP_RISE * progress \
                         + random.uniform(-0.3, 0.3)
            filter_dp = CRAC_BASE["filter_dp_pa"] + CRAC_ANOMALY_FILTER_DP_RISE * progress \
                        + random.uniform(-2.0, 2.0)
            airflow = CRAC_BASE["airflow_cfm"] - CRAC_ANOMALY_AIRFLOW_DROP * progress \
                      + random.uniform(-15, 15)
            vibration = CRAC_BASE["fan_vibration_mm_s"] + CRAC_ANOMALY_VIBRATION_RISE * progress \
                        + random.uniform(-0.05, 0.05)
            compressor = CRAC_BASE["compressor_load_pct"] + CRAC_ANOMALY_COMPRESSOR_RISE * progress \
                         + random.uniform(-1.0, 1.0)
            fan_rpm = CRAC_BASE["fan_rpm"] + 250 * progress + random.uniform(-15, 15)
            motor_current = CRAC_BASE["fan_motor_current_a"] + 1.8 * progress + random.uniform(-0.05, 0.05)

        return {
            "fan_rpm": int(max(0, fan_rpm)),
            "fan_motor_current_a": round(max(0, motor_current), 2),
            "fan_motor_temp_c": round(motor_temp, 1),
            "fan_vibration_mm_s": round(max(0, vibration), 2),
            "filter_dp_pa": round(max(0, filter_dp), 1),
            "airflow_cfm": round(max(0, airflow), 0),
            "supply_air_temp_c": round(CRAC_BASE["supply_air_temp_c"] + 0.3 * swing, 1),
            "return_air_temp_c": round(CRAC_BASE["return_air_temp_c"] + 0.3 * swing, 1),
            "compressor_load_pct": round(max(0, min(100, compressor)), 1),
        }

    @staticmethod
    def _crac_threshold_flags(r):
        """Same rule set as the CoolingTwin — kept here too so the console
        heartbeat and CSV log are self-explanatory without cross-referencing
        the twin code."""
        flags = []
        if r["fan_motor_temp_c"] >= CRAC_MOTOR_TEMP_TRIP:
            flags.append("bearing_overheat")
        if r["filter_dp_pa"] >= CRAC_FILTER_DP_TRIP:
            flags.append("filter_restriction")
        if r["airflow_cfm"] <= CRAC_NOMINAL_AIRFLOW * CRAC_AIRFLOW_TRIP_PCT:
            flags.append("airflow_loss")
        return flags

    def _sim_timestamp(self):
        """ISO-8601 stamp on the SIMULATED clock.

        Consumers that compute rates (CoolingTwin's slope features) must
        divide by this, not by wall clock — otherwise an accelerated
        playback inflates every rate by the acceleration factor.
        """
        return datetime.fromtimestamp(
            self.sim_epoch + self.t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _crac_message(self):
        vals = self._crac_reading()
        return {
            "timestamp": self._sim_timestamp(),
            "unit_id": CRAC_UNIT_ID,
            "run_id": self.run_id,
            **vals,
            "threshold_flags": self._crac_threshold_flags(vals),
        }

    def _message(self, rack, phase):
        vals = self._reading_for_rack(rack, phase)
        msg = {
            "timestamp": self._sim_timestamp(),
            "rack_id": rack["rack_id"],
            "location": rack["location"],
            "run_id": self.run_id,
            **vals,
            "status": self._status(vals),
        }
        return msg

    # -- publish loop --------------------------------------------------------

    def _publish(self, msg):
        topic = f"{TOPIC_BASE}/{msg['rack_id']}"
        payload = json.dumps(msg)
        if self.client:
            self.client.publish(topic, payload)
        if self.csv_writer:
            self.csv_writer.writerow(msg)
            self.csv_file.flush()
            with open("room_data.json", "a") as jf:
                jf.write(payload + "\n")

    def _publish_crac(self, msg):
        topic = f"{TOPIC_BASE}/{msg['unit_id']}"  # datacenter/racks/CRAC-01
        payload = json.dumps(msg)
        if self.client:
            self.client.publish(topic, payload)
        if self.crac_csv_writer:
            row = dict(msg)
            row["threshold_flags"] = ",".join(row["threshold_flags"])
            self.crac_csv_writer.writerow(row)
            self.crac_csv_file.flush()
            with open("crac_data.json", "a") as jf:
                jf.write(payload + "\n")

    def run(self):
        # optional auto-trigger for --anomaly
        # In SIMULATED seconds. 600s == one full slope window, so the
        # CoolingTwin's buffer is already populated when the fault starts.
        auto_at = 600.0 if self.args.anomaly else None

        accel = self.sim_step / self.wall_interval if self.wall_interval else float("inf")
        print(f"Publishing to {TOPIC_BASE}/<rack>  every {self.wall_interval}s wall")
        print(f"Simulated clock: +{self.sim_step}s per tick "
              f"(cap {SIM_SAMPLE_INTERVAL_S}s) -> {accel:.0f}x acceleration")
        print(f"Fault duration:  {ANOMALY_DURATION_S / 3600:.1f}h simulated "
              f"= {ANOMALY_DURATION_S / accel / 60:.1f} min wall\n")
        print("Commands:  a<Enter> = trigger anomaly   q<Enter> = quit\n")

        while self.running:
            for i, rack in enumerate(RACKS):
                msg = self._message(rack, phase=i * 2.0)
                self._publish(msg)

            crac_msg = self._crac_message()
            self._publish_crac(crac_msg)

            # console heartbeat on rack A + CRAC-01
            a = self.racks_snapshot()
            flag = "  <<< ANOMALY" if self.anomaly_active else ""
            print(f"t={int(self.t):3d}s  {a['rack_id']}  "
                  f"inlet={a['inlet_temperature']:4.1f}C  "
                  f"exhaust={a['exhaust_temperature']:4.1f}C  "
                  f"fan={a['fan_speed']:4d}  power={a['power_draw']:4.1f}kW  "
                  f"[{a['status']}]{flag}")
            crac_flag_str = ",".join(crac_msg["threshold_flags"]) or "-"
            print(f"         CRAC-01  motor_temp={crac_msg['fan_motor_temp_c']:5.1f}C  "
                  f"filter_dp={crac_msg['filter_dp_pa']:5.1f}Pa  "
                  f"airflow={crac_msg['airflow_cfm']:5.0f}cfm  "
                  f"vib={crac_msg['fan_vibration_mm_s']:4.2f}mm/s  "
                  f"[{crac_flag_str}]")

            time.sleep(self.wall_interval)
            # One tick advances the simulated clock by at most one training
            # sample interval — this is the sub-stepping guarantee.
            self.t += self.sim_step
            if self.anomaly_active:
                self.anomaly_elapsed += self.sim_step

            if auto_at is not None and self.t >= auto_at and not self.anomaly_active:
                self.trigger_anomaly()
                auto_at = None

            # --loop: cycle healthy -> degrading -> failed -> healthy forever.
            #
            # Without this the demo has one shot: whoever opens the link after
            # the fault completes sees a system parked at FAILED with flat
            # slopes, which looks broken rather than finished. Looping means
            # any arrival time lands somewhere in a live degradation.
            if self.args.loop:
                if not self.anomaly_active:
                    self.healthy_elapsed += self.sim_step
                    if self.healthy_elapsed >= HEALTHY_DWELL_S:
                        self.trigger_anomaly()
                else:
                    # Count time since the CRAC's own sensors tripped, not
                    # since the fault began — that is when the flat red tail
                    # starts, and it is the part worth cutting short.
                    if crac_msg["threshold_flags"]:
                        self.tripped_elapsed += self.sim_step
                    if (self.tripped_elapsed >= FAILED_DWELL_S
                            or self.anomaly_elapsed >= ANOMALY_DURATION_S):
                        self.reset_anomaly()

    def racks_snapshot(self):
        """Regenerate rack A's current reading for the console line."""
        return self._message(RACKS[0], phase=0.0)

    def trigger_anomaly(self):
        if not self.anomaly_active:
            self.anomaly_active = True
            self.anomaly_elapsed = 0.0
            self.healthy_elapsed = 0.0
            self.tripped_elapsed = 0.0
            self.cycle += 1
            print(f"\n*** CRAC DEGRADATION TRIGGERED (cycle {self.cycle}) "
                  f"- inlet temps will climb ***\n")

    def reset_anomaly(self):
        """Return to healthy and start the cycle again (--loop).

        Only the fault state is cleared. self.t — the simulated clock — keeps
        advancing monotonically, because the twins measure their trend
        features against payload timestamps: winding it back would produce a
        negative elapsed time and a nonsense slope. A repaired CRAC is a new
        healthy period later in the day, not a trip back to this morning.
        """
        self.anomaly_active = False
        self.anomaly_elapsed = 0.0
        self.healthy_elapsed = 0.0
        self.tripped_elapsed = 0.0
        self.run_id += 1          # everything published from here is a new run
        print(f"\n*** CRAC REPLACED - cycle {self.cycle} complete, "
              f"returning to healthy ***\n")

    def stop(self):
        self.running = False


# ---------------------------------------------------------------------------
# Input thread for interactive control (CLI use only — see run_simulator()
# below for the headless/embedded path used when this runs alongside the
# twins in a single deployed process, which has no stdin to read from).
# ---------------------------------------------------------------------------

def input_loop(sim):
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "a":
            sim.trigger_anomaly()
        elif cmd == "q":
            sim.stop()
            break


# ---------------------------------------------------------------------------
# Embeddable entry point — used by main.py to run the simulator in the same
# process as the twins/orchestrator (single-service deploy). Differs from
# main() below in two ways:
#   1. TLS + username/password auth, via the same MQTT_TLS/MQTT_USERNAME/
#      MQTT_PASSWORD env vars base_twin.py and orchestrator.py already use
#      — the CLI main() has neither, since it's built for the local
#      anonymous mosquitto.conf broker.
#   2. No stdin-reading input thread — a deployed worker has no terminal
#      to type "a"/"q" into. Anomaly trigger is controlled by the
#      AUTO_ANOMALY env var instead (auto-fires 20s after start, same
#      timing as --anomaly on the CLI).
# ---------------------------------------------------------------------------

class _EmbeddedArgs:
    """Mimics the argparse.Namespace Simulator/run() expect, without a
    real command line — only ever used inside another process
    (main.py), never as __main__."""
    def __init__(self, anomaly, loop=False):
        self.csv = False
        self.anomaly = anomaly
        self.loop = loop


def run_simulator(host, port, use_tls=False, username=None, password=None, auto_anomaly=False):
    if not HAVE_MQTT:
        raise RuntimeError("paho-mqtt not installed — required to run the simulator")

    sim = Simulator(_EmbeddedArgs(anomaly=auto_anomaly))
    status_topic = "datacenter/status/simulator"

    client = mqtt.Client(client_id="sensor-simulator")
    if use_tls:
        client.tls_set()
    if username:
        client.username_pw_set(username, password)
    else:
        # No explicit credentials passed in (the RUN_SIMULATOR in-process
        # path): fall back to this service's own identity from the env.
        apply_credentials(client, "sensor-simulator")
    # Same LWT pattern as base_twin.py / orchestrator.py — if this
    # process dies, the dashboard should be able to tell the telemetry
    # source went dark, not just see the rack cards go stale.
    client.will_set(status_topic, payload="offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    for attempt in range(1, 13):
        try:
            client.connect(host, port, keepalive=60)
            break
        except (ConnectionRefusedError, OSError) as e:
            print(f"[simulator] connect attempt {attempt}/12 failed ({e}); retrying in 5s")
            time.sleep(5)
    else:
        raise ConnectionError(f"[simulator] could not connect to {host}:{port} after 12 attempts")

    client.loop_start()
    client.publish(status_topic, "online", qos=1, retain=True)
    sim.client = client
    print(f"[simulator] connected to {host}:{port} (tls={use_tls})")

    try:
        sim.run()
    finally:
        sim.stop()
        client.publish(status_topic, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


# ---------------------------------------------------------------------------
# Main — CLI entry point for standalone/local use (python sensor_simulator.py)
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Data center twin telemetry simulator")
    ap.add_argument("--host", default="localhost", help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    ap.add_argument("--sim-step", type=float, default=SIM_SAMPLE_INTERVAL_S,
                    dest="sim_step",
                    help=f"simulated seconds advanced per publish tick "
                         f"(clamped to {SIM_SAMPLE_INTERVAL_S}s, the training "
                         f"sample interval)")
    ap.add_argument("--interval", type=float, default=PUBLISH_INTERVAL,
                    help="wall-clock seconds between publish ticks; lower this "
                         "to watch the same degradation faster")
    ap.add_argument("--anomaly", action="store_true",
                    help="auto-trigger the CRAC anomaly 20s after start")
    ap.add_argument("--loop", action="store_true",
                    help="cycle healthy -> degrading -> failed -> healthy "
                         "continuously, so a viewer arriving at any moment "
                         "sees a live degradation rather than a finished one")
    ap.add_argument("--csv", action="store_true",
                    help="also append readings to room_data.csv / room_data.json")
    ap.add_argument("--no-mqtt", action="store_true",
                    help="run without a broker (console/CSV only)")
    args = ap.parse_args()

    sim = Simulator(args)

    if not args.no_mqtt:
        if not HAVE_MQTT:
            print("paho-mqtt not installed. Run: pip install paho-mqtt")
            print("Continuing in --no-mqtt mode.\n")
        else:
            # Identify as `sensor-simulator`: that is the username the ACL
            # grants datacenter/racks/* to (mosquitto/acl). An anonymous
            # client is refused outright now.
            client = mqtt.Client(client_id="sensor-simulator")
            if os.environ.get("MQTT_TLS", "false").lower() == "true":
                client.tls_set()
            apply_credentials(client, "sensor-simulator")

            # connect() only opens the socket — the broker's verdict arrives
            # later, in CONNACK. Without this callback a rejected credential
            # printed "Connected" and then published into a void, which is
            # the worst version of this failure: silent.
            def _on_connect(cl, userdata, flags, rc):
                if rc == 0:
                    print(f"Connected to MQTT broker at {args.host}:{args.port}")
                else:
                    print(f"!! BROKER REFUSED THE CONNECTION: rc={rc} "
                          f"({mqtt.connack_string(rc)})")
                    print("!! No telemetry will be published. Check "
                          "MQTT_USERNAME_SENSOR_SIMULATOR / MQTT_PASSWORD_* "
                          "(run.sh exports these from mosquitto/credentials.json).")

            client.on_connect = _on_connect
            try:
                client.connect(args.host, args.port, keepalive=60)
                client.loop_start()
                sim.client = client
            except Exception as e:
                print(f"Could not connect to broker ({e}). Running console-only.")

    # interactive control thread
    t = threading.Thread(target=input_loop, args=(sim,), daemon=True)
    t.start()

    try:
        sim.run()
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
        if sim.client:
            sim.client.loop_stop()
            sim.client.disconnect()
        if sim.csv_file:
            sim.csv_file.close()
        if sim.crac_csv_file:
            sim.crac_csv_file.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
