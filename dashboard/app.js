/* Live wiring for the operator dashboard.
 *
 * Connects as `viewer`, an identity the broker ACL allows to read four topic
 * families and to write nothing at all (mosquitto/acl) — which is what makes
 * shipping this credential in a public page acceptable.
 *
 * STRUCTURE, kept from the design reference: the DOM is built ONCE at load
 * and paint() only assigns values, widths and colours to existing nodes.
 * Rewriting innerHTML per message would restart every CSS transition, so the
 * meters would jump instead of sliding and the log rows would re-animate.
 * The only innerHTML writes happen in the build phase and in addLog(), which
 * prepends a new row and never touches existing ones.
 *
 * Nothing here computes risk or health. Every value displayed arrived on the
 * wire; the twins and the orchestrator decide, this page renders.
 */
(function () {
  "use strict";

  var cfg = window.DCDT_CONFIG || {};
  var $ = function (i) { return document.getElementById(i); };
  var clamp = function (v, a, b) { return Math.max(a, Math.min(b, v)); };

  var ACTION = 0.5;
  var RACKS = ["SR-RACK-01", "SR-RACK-02", "SR-RACK-03"];

  // Trip points, taken from the code that actually raises the flags
  // (twins/cooling_twin.py `_check_thresholds`) — not invented for display.
  var MOTOR_TRIP = 105.0;      // fan_motor_temp_c >= 105 -> bearing_overheat
  var DP_TRIP = 350.0;         // filter_dp_pa     >= 350 -> filter_restriction
  var AIRFLOW_NOMINAL = 3400.0;
  var AIRFLOW_TRIP = 2210.0;   // 65% of nominal          -> airflow_loss

  // Only these three CRAC signals have a defined trip limit, so only these
  // three get a meter. The rest are shown as values: drawing a bar implies a
  // limit to be measured against, and inventing one would be a lie in CSS.
  var METERED = [
    ["Motor winding", "fan_motor_temp_c", "°C", 1, MOTOR_TRIP, "up"],
    ["Filter ΔP", "filter_dp_pa", " Pa", 0, DP_TRIP, "up"],
    ["Airflow", "airflow_cfm", " CFM", 0, AIRFLOW_TRIP, "down"]
  ];
  var PLAIN = [
    ["Fan speed", "fan_rpm", " rpm", 0],
    ["Motor current", "fan_motor_current_a", " A", 2],
    ["Vibration", "fan_vibration_mm_s", " mm/s", 2],
    ["Supply air", "supply_air_temp_c", "°C", 1],
    ["Return air", "return_air_temp_c", "°C", 1],
    ["Compressor load", "compressor_load_pct", " %", 0]
  ];

  var ASSETS = [["CRAC-01", "cooling"]]
    .concat(RACKS.map(function (r) { return [r, "rack-" + r]; }))
    .concat([["Occupancy", "occupancy"], ["Energy", "energy"]]);

  var ENERGY = [["IT power", "it_power_kw", " kW", 1],
                ["CRAC compressor", "crac_compressor_kw", " kW", 2],
                ["CRAC fan", "crac_fan_kw", " kW", 2],
                ["CRAC total", "crac_power_kw", " kW", 2],
                ["Room total", "total_power_kw", " kW", 1],
                ["PUE", "pue", "", 2]];
  var TRIPS = ["bearing_overheat", "filter_restriction", "airflow_loss"];

  var col = function (s) {
    return ({ healthy: "var(--ok)", watch: "var(--cool)", degrading: "var(--warn)",
              tripped: "var(--trip)", offline: "var(--faint)" })[s] || "var(--faint)";
  };
  var reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;

  /* ---------------------------------------------------------------- state */
  var run = { start: null, mAt: null, rAt: null, now: null };
  var buf = [];                 // {s: simulated seconds, p: probability}
  var msgs = 0, smooth = null;
  var state = { cooling: null, prediction: null, energy: null, racks: {}, offline: {} };

  // The degradation run whose messages are current. Every publisher stamps
  // run_id (sensor_simulator.py), and the decision topics are RETAINED, so a
  // page opened during a healthy stretch is handed the PREVIOUS run's
  // recommendation by the broker. Anything stamped with an older run is
  // discarded rather than rendered as live state.
  var currentRun = null;
  var lastRec = null;

  // How much simulated time the timeline rail spans. A cycle is ~30 min
  // healthy + ~96 min rising + ~15 min tripped; the rail grows if a run
  // outlives that so "now" never pins to the right-hand edge.
  var RAIL_MIN_SECONDS = 150 * 60;
  function railSpan() {
    if (run.start == null || run.now == null) return RAIL_MIN_SECONDS;
    return Math.max(RAIL_MIN_SECONDS, (run.now - run.start) * 1.05);
  }
  function pos(simSeconds) {
    if (run.start == null) return 0;
    return clamp((simSeconds - run.start) / railSpan(), 0, 1);
  }
  var pc = function (v) { return (v * 100).toFixed(2) + "%"; };

  // Returns true when a message belongs to a run that has already ended.
  function isStale(msg) {
    var rid = msg && msg.run_id;
    if (rid == null || currentRun == null) return false;   // unstamped: cannot judge
    return rid < currentRun;
  }

  function noteRun(msg) {
    var rid = msg && msg.run_id;
    if (rid == null) return;
    if (currentRun == null) { currentRun = rid; return; }
    if (rid > currentRun) {
      currentRun = rid;
      run = { start: null, mAt: null, rAt: null, now: null };
      buf = [];
      lastRec = null;                 // the previous run's advice is void
      addLog("sec", "New degradation run — previous run's state cleared");
    }
  }
  var fmtMin = function (sec) { return Math.round(sec / 60) + " min"; };

  /* -------------------------------------------------- build the DOM ONCE */
  $("assets").innerHTML = ASSETS.map(function (a, i) {
    return '<div class="as"><div class="b" id="ab' + i + '"></div>' +
           '<div class="n">' + a[0] + '</div><div class="s" id="as' + i + '">—</div></div>';
  }).join("");

  $("tele").innerHTML =
    METERED.map(function (m) {
      return '<div class="tl"><div class="top"><span class="lab">' + m[0] + '</span>' +
             '<span class="val" id="tv_' + m[1] + '">—</span></div>' +
             '<div class="meter"><em id="tm_' + m[1] + '"></em></div></div>';
    }).join("") +
    '<div class="note">Bars show distance to the trip point that raises each flag. ' +
    'The signals below have no defined trip limit, so they are reported as values only.</div>' +
    PLAIN.map(function (p) {
      return '<div class="kv"><span>' + p[0] + '</span><b id="tv_' + p[1] + '">—</b></div>';
    }).join("");

  $("energy").innerHTML = ENERGY.map(function (e) {
    return '<div class="kv"><span>' + e[0] + '</span><b id="ev_' + e[1] + '">—</b></div>';
  }).join("") +
  '<div class="note">Fan power by the affinity law from measured shaft speed ' +
  '(\u221d rpm\u00b3); compressor linear in reported load. Coefficients and ' +
  'their derivation are in the README \u2014 engineering estimates for a ' +
  'modelled unit, not measurements of a physical CRAC.</div>';

  $("trips").innerHTML = TRIPS.map(function (n) {
    return '<span class="trip" id="tp_' + n + '">' + n + "</span>";
  }).join("");

  /* ------------------------------------------------------------------ log */
  function addLog(k, m) {
    var box = $("log"), row = document.createElement("div");
    row.className = "row";
    row.innerHTML = '<div class="t"></div><div class="k ' + k + '"></div><div class="m"></div>';
    row.children[0].textContent = new Date().toLocaleTimeString("en-GB", { hour12: false });
    row.children[1].textContent = k === "pred" ? "predict" : k === "act" ? "action" : "system";
    row.children[2].textContent = m;
    box.prepend(row);
    while (box.children.length > 60) box.lastChild.remove();
  }

  /* --------------------------------------------- is this advice still true? */
  // A recommendation is a claim about the room RIGHT NOW ("motor temperature
  // at/above the Class F alarm point"). Retained on its topic, it outlives
  // the condition that produced it, so it must be re-checked against current
  // telemetry before being shown — otherwise the page asserts a bearing
  // inspection while the winding reads 72 °C and the banner says all clear.
  function recIsCurrent(rec) {
    if (!rec) return false;
    if (rec.run_id != null && currentRun != null && rec.run_id !== currentRun) return false;

    // The orchestrator only issues advice above the action threshold
    // (should_recommend in orchestrator/orchestrator.py). If the live
    // probability is below it, no recommendation can be current.
    var p = state.prediction;
    if (!p || p.failure_probability < ACTION) return false;

    // Every sensor trip the advice was based on must still be tripped.
    var now = (state.cooling && state.cooling.threshold_flags) || [];
    var then = rec.threshold_flags || [];
    return then.every(function (f) { return now.indexOf(f) >= 0; });
  }

  function renderRecommendation() {
    if (recIsCurrent(lastRec)) {
      $("verb").textContent = (lastRec.action || "—").replace(/_/g, " ");
      $("verb").style.color = "var(--ink)";
      $("why").textContent = lastRec.rationale || "";
      var bits = [];
      if (lastRec.failure_probability != null) bits.push("p=" + lastRec.failure_probability);
      if (lastRec.seq != null) bits.push("decision #" + lastRec.seq);
      $("meta").textContent = bits.join(" · ");
    } else {
      $("verb").textContent = "Monitor";
      $("verb").style.color = "var(--dim)";
      $("why").textContent = "No action required. No current recommendation for this run.";
      $("meta").textContent = "";
    }
  }

  /* ---------------------------------------------------------------- paint */
  function assetState(key) {
    if (state.offline[key]) return "offline";
    if (key === "cooling") {
      var c = state.cooling, p = state.prediction;
      if (!c) return "offline";
      if ((c.threshold_flags || []).length) return "tripped";
      if (p && p.failure_probability >= ACTION) return "degrading";
      if (p && p.failure_probability >= 0.15) return "watch";
      return "healthy";
    }
    if (key.indexOf("rack-") === 0) {
      var r = state.racks[key];
      if (!r) return "offline";
      var s = (r.status || "").toUpperCase();
      return s === "CRITICAL" ? "tripped" : s === "WARNING" ? "degrading" : "healthy";
    }
    if (key === "energy") return state.energy ? "healthy" : "offline";
    return state.seenOccupancy ? "healthy" : "offline";
  }

  function paint() {
    var p = state.prediction;
    var cooling = state.cooling || {};
    var flags = cooling.threshold_flags || (p && p.contributing_factors) || [];

    /* — risk — */
    if (p) {
      var prob = p.failure_probability;
      smooth = (reduce || smooth === null) ? prob : smooth + (prob - smooth) * 0.3;
      var st = prob < 0.15 ? "healthy" : prob < ACTION ? "watch" : prob < 0.9 ? "degrading" : "tripped";
      $("prob").textContent = (smooth * 100).toFixed(1);
      $("prob").style.color = col(st);
      $("dot").style.background = col(st);

      // null below the action threshold BY DESIGN (inference/model_loader.py):
      // the regressor has no training support there, so a countdown would be
      // fabricated. Render a dash, never 0.
      $("ttf").textContent = (p.time_to_failure_hours == null)
        ? "—" : p.time_to_failure_hours.toFixed(2) + " h";

      // predicted_mode is rule-derived from the sensor flags, not a model
      // output — the artefact has no mode classifier. Say so on the chip.
      var chip = $("modeChip");
      if (p.predicted_mode && p.predicted_mode !== "NONE") {
        chip.textContent = "failure mode · " + p.predicted_mode +
                           (p.predicted_mode_basis === "rule" ? " · rule-derived" : "");
        chip.style.display = "";
      } else {
        chip.style.display = "none";
      }
    }

    /* — warning timeline: the point of the page — */
    if (run.start != null && run.now != null) {
      $("now").style.left = pc(pos(run.now));
      $("mkM").style.left = run.mAt != null ? pc(pos(run.mAt)) : "-999px";
      $("mkR").style.left = run.rAt != null ? pc(pos(run.rAt)) : "-999px";

      // Both ends are SIMULATED timestamps carried on the prediction, so the
      // gap is measured on the clock the model's features were measured on.
      var end = run.rAt != null ? run.rAt : (run.mAt != null ? run.now : null);
      if (run.mAt != null && end != null) {
        var gapSec = Math.max(0, end - run.mAt);
        $("gap").style.left = pc(pos(run.mAt));
        $("gap").style.width = pc(Math.max(0, pos(end) - pos(run.mAt)));
        $("glab").style.left = pc((pos(run.mAt) + pos(end)) / 2);
        $("glab").textContent = fmtMin(gapSec) + " of warning";
        $("leadv").textContent = fmtMin(gapSec) + (run.rAt == null ? " +" : "");
      } else {
        $("gap").style.width = "0";
        $("glab").textContent = "";
        $("leadv").textContent = "—";
      }

      $("say").innerHTML =
        run.mAt == null ? "No fault developing. Model quiet, thresholds quiet."
        : run.rAt == null ? "Model has warned. Threshold rules still <b>silent</b>."
        : "Model warned <b>" + fmtMin(run.rAt - run.mAt) +
          "</b> before any threshold rule fired.";
    }

    /* — room — */
    ASSETS.forEach(function (a, i) {
      var s = assetState(a[1]);
      $("ab" + i).style.background = col(s);
      var e = $("as" + i);
      e.textContent = s;
      e.style.color = col(s);
    });

    /* — CRAC telemetry — */
    METERED.forEach(function (m) {
      var v = cooling[m[1]];
      var el = $("tv_" + m[1]);
      if (v == null) { el.textContent = "—"; return; }
      el.textContent = v.toFixed(m[3]) + m[2];
      var f = m[5] === "down"
        ? clamp((AIRFLOW_NOMINAL - v) / (AIRFLOW_NOMINAL - m[4]), 0, 1)
        : clamp(v / m[4], 0, 1);
      var c = f >= 1 ? "var(--trip)" : f > 0.8 ? "var(--warn)" : "var(--cool)";
      el.style.color = c;
      var bar = $("tm_" + m[1]);
      bar.style.width = (f * 100).toFixed(1) + "%";
      bar.style.background = c;
    });
    PLAIN.forEach(function (q) {
      var v = cooling[q[1]];
      $("tv_" + q[1]).textContent = v == null ? "—" : v.toFixed(q[3]) + q[2];
    });

    /* — energy — */
    if (state.energy) {
      ENERGY.forEach(function (e) {
        var v = state.energy[e[1]];
        $("ev_" + e[1]).textContent = (v == null) ? "—" : v.toFixed(e[3]) + e[2];
      });
    }

    /* — trips — */
    TRIPS.forEach(function (n) { $("tp_" + n).classList.toggle("on", flags.indexOf(n) >= 0); });

    // Re-checked every paint, not just on arrival: the advice can go stale
    // because the ROOM changed, with no new message on its own topic.
    renderRecommendation();

    $("msgs").textContent = msgs.toLocaleString() + " msgs";
    if (run.now != null) {
      var t = Math.floor(run.now % 86400);
      var two = function (n) { return n < 10 ? "0" + n : "" + n; };
      $("clock").textContent = two(Math.floor(t / 3600)) + ":" +
        two(Math.floor(t % 3600 / 60)) + ":" + two(t % 60) + " sim";
    }
    draw();
  }

  /* ---------------------------------------------------- chart (hand-drawn) */
  var g = null;
  function draw() {
    var svg = $("svg"), w = svg.clientWidth || 600, h = svg.clientHeight || 220;
    var X = function (v) { return v * w; }, Y = function (v) { return h - 5 - v * (h - 14); };
    if (!g || g.w !== w || g.h !== h) {
      svg.setAttribute("viewBox", "0 0 " + w + " " + h);
      svg.innerHTML =
        '<defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0" stop-color="#4FB0C6" stop-opacity=".28"/>' +
        '<stop offset="1" stop-color="#4FB0C6" stop-opacity="0"/></linearGradient></defs>' +
        [0, .25, .5, .75, 1].map(function (v) {
          return '<line x1="0" y1="' + Y(v) + '" x2="' + w + '" y2="' + Y(v) + '" stroke="#1D2937"/>';
        }).join("") +
        '<rect id="band" x="0" y="0" width="0" height="' + h + '" fill="#4FB0C6" opacity=".07"/>' +
        '<line x1="0" y1="' + Y(ACTION) + '" x2="' + w + '" y2="' + Y(ACTION) +
        '" stroke="#E0A03A" stroke-dasharray="4 4"/>' +
        '<line id="vr" y1="0" y2="' + h + '" stroke="#D65C54" stroke-width="1.5" opacity="0"/>' +
        '<line id="vm" y1="0" y2="' + h + '" stroke="#4FB0C6" stroke-width="1.5" opacity="0"/>' +
        '<path id="area" fill="url(#fill)"/>' +
        '<path id="line" fill="none" stroke="#4FB0C6" stroke-width="2" stroke-linejoin="round"/>';
      g = { w: w, h: h, area: svg.querySelector("#area"), line: svg.querySelector("#line"),
            vm: svg.querySelector("#vm"), vr: svg.querySelector("#vr"),
            band: svg.querySelector("#band") };
    }
    if (!buf.length) { g.line.setAttribute("d", ""); g.area.setAttribute("d", ""); return; }
    var d = buf.map(function (q, i) {
      return (i ? "L" : "M") + X(pos(q.s)).toFixed(1) + "," + Y(q.p).toFixed(1);
    }).join("");
    g.line.setAttribute("d", d);
    g.area.setAttribute("d", d + "L" + X(pos(buf[buf.length - 1].s)).toFixed(1) + "," + h +
                         "L" + X(pos(buf[0].s)).toFixed(1) + "," + h + "Z");
    var set = function (el, x) {
      el.setAttribute("x1", x); el.setAttribute("x2", x); el.setAttribute("opacity", "1");
    };
    run.mAt != null ? set(g.vm, X(pos(run.mAt))) : g.vm.setAttribute("opacity", "0");
    run.rAt != null ? set(g.vr, X(pos(run.rAt))) : g.vr.setAttribute("opacity", "0");
    if (run.mAt != null) {
      var e = run.rAt != null ? run.rAt : run.now;
      g.band.setAttribute("x", X(pos(run.mAt)));
      g.band.setAttribute("width", Math.max(0, X(pos(e)) - X(pos(run.mAt))));
    } else { g.band.setAttribute("width", 0); }
  }

  /* ----------------------------------------------------------- prediction */
  function onPrediction(p) {
    var sim = p.sim_time;
    if (sim == null) return;                 // no simulated clock, no timeline
    var flags = p.threshold_flags || p.contributing_factors || [];

    if (run.start == null) run.start = sim;

    // New run: normally announced by run_id (noteRun). This probability-based
    // fallback only applies to payloads with no run_id at all — a retained
    // message published before the stamp existed.
    if (p.run_id == null &&
        p.failure_probability < 0.2 && !flags.length && (run.mAt != null || run.rAt != null)) {
      run = { start: sim, mAt: null, rAt: null, now: sim };
      buf = [];
      addLog("sec", "Run reset — CRAC restored, healthy baseline");
    }

    run.now = sim;
    state.prediction = p;

    if (run.mAt == null && p.failure_probability >= ACTION) {
      run.mAt = sim;
      addLog("pred", "Model crossed the action threshold — no sensor has tripped");
    }
    if (run.rAt == null && flags.length) {
      run.rAt = sim;
      addLog("act", "Threshold rule tripped: " + flags.join(", ") +
                    (run.mAt != null ? " — model led by " + fmtMin(sim - run.mAt) : ""));
    }

    buf.push({ s: sim, p: p.failure_probability });
    while (buf.length > 600) buf.shift();
    paint();
  }

  /* ----------------------------------------------------------------- mqtt */
  var secure = location.protocol === "https:";
  var host = cfg.brokerHost || location.hostname || "localhost";
  var port = cfg.brokerPort || (secure ? 8443 : 9001);
  var BROKER = (secure ? "wss://" : "ws://") + host + ":" + port + (cfg.path || "/mqtt");
  $("broker").textContent = (secure ? "wss://" : "ws://") + host + ":" + port;

  // Any component reporting offline degrades the header indicator. This is
  // the only place status/# is surfaced.
  function updateIndicator() {
    var down = Object.keys(state.offline).filter(function (k) { return state.offline[k]; });
    if (!connected) return;
    if (down.length) {
      $("conn").textContent = down.length + " component" + (down.length > 1 ? "s" : "") + " offline";
      $("conn").classList.remove("on");
    } else {
      $("conn").textContent = "live";
      $("conn").classList.add("on");
    }
  }

  var connected = false;
  function setConn(on, text) {
    connected = on;
    $("conn").textContent = text;
    $("conn").classList.toggle("on", on);
    if (!on) $("dot").style.background = col("offline");
  }

  if (!cfg.username) {
    setConn(false, "no credentials");
    addLog("sec", "dashboard/config.js missing — run ./serve_demo.sh");
    return;
  }

  setConn(false, "connecting");
  var client = mqtt.connect(BROKER, {
    username: cfg.username, password: cfg.password,
    clientId: "viewer-" + Math.random().toString(16).slice(2, 8),
    reconnectPeriod: 3000, connectTimeout: 8000, clean: true
  });

  client.on("connect", function () {
    setConn(true, "live");
    addLog("sec", "connected as " + cfg.username + " · read-only by ACL");
    ["datacenter/twin-state/+", "datacenter/predictions/+",
     "datacenter/recommendations/+", "datacenter/status/+"].forEach(function (t) {
      client.subscribe(t, { qos: 0 });
    });
  });
  client.on("reconnect", function () { setConn(false, "reconnecting"); });
  client.on("close", function () { setConn(false, "disconnected"); });
  client.on("error", function (e) {
    setConn(false, "error");
    addLog("sec", String(e && e.message ? e.message : e));
  });

  // Routing, once a message is known to be current.
  function route(topic, msg) {
    if (topic === "datacenter/twin-state/cooling") { state.cooling = msg; paint(); }
    else if (topic.indexOf("datacenter/twin-state/rack-") === 0) {
      state.racks[topic.substr("datacenter/twin-state/".length)] = msg; paint();
    }
    else if (topic === "datacenter/twin-state/energy") { state.energy = msg; paint(); }
    else if (topic === "datacenter/twin-state/occupancy") { state.seenOccupancy = true; paint(); }
    else if (topic.indexOf("datacenter/predictions/") === 0) onPrediction(msg);
    else if (topic.indexOf("datacenter/recommendations/") === 0) {
      // Stored, not rendered directly. renderRecommendation() decides whether
      // it still describes the room. estimated_cost stays unrendered — it is
      // a documented placeholder returning None in orchestrator/rules.py.
      lastRec = msg;
      if (recIsCurrent(msg)) addLog("act", (msg.action || "").replace(/_/g, " "));
      renderRecommendation();
    }
  }

  // Retained messages held until a LIVE message says which run is current.
  //
  // The broker hands every new subscriber the last message on each topic.
  // Those arrive before anything live, so the first one would otherwise
  // define "the current run" — and if the page was opened during a healthy
  // stretch, that first message is the FINISHED run's 78% risk and its
  // bearing-inspection advice. Holding them until a live message arrives
  // (well under a second at normal cadence) means a retained message is only
  // ever rendered once it is known to belong to the run still in progress.
  var pending = [];
  var established = false;

  client.on("message", function (topic, payload, packet) {
    msgs += 1;
    var text = payload.toString();
    var retained = !!(packet && packet.retain);

    if (topic.indexOf("datacenter/status/") === 0) {
      // LWT/status is CONNECTION STATE, not an event. These arrive retained,
      // so an "offline" left behind by a process that died hours ago would
      // otherwise be logged as if it had just happened — timestamped now,
      // next to live data. It drives the indicator and nothing else.
      var who = topic.split("/").pop();
      if (who !== "simulator") state.offline[who] = (text === "offline");
      updateIndicator();
      paint();
      return;
    }

    var msg;
    try { msg = JSON.parse(text); } catch (e) { return; }

    if (!established) {
      if (retained) { pending.push([topic, msg]); return; }
      established = true;
      if (msg.run_id != null) currentRun = msg.run_id;
      // Replay only the held messages belonging to the run now in progress.
      var keep = pending.filter(function (e) {
        return currentRun == null || e[1].run_id === currentRun;
      });
      var dropped = pending.length - keep.length;
      pending = [];
      keep.forEach(function (e) { route(e[0], e[1]); });
      if (dropped) addLog("sec", dropped + " retained message" + (dropped > 1 ? "s" : "") +
                                 " from a finished run discarded");
    }

    noteRun(msg);
    if (isStale(msg)) return;
    route(topic, msg);
  });

  addEventListener("resize", function () { g = null; draw(); });
  draw();
})();
