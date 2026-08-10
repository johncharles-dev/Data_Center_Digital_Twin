"""Append-only audit trail of every decision the system makes.

Records three event types — predictions, recommendations, acknowledgements —
to logs/audit.jsonl, one JSON object per line.

Two properties make this an audit trail rather than a log file:

**Per-source sequence numbers.** Producers stamp their own `seq`
(orchestrator/orchestrator.py `_stamp`, acknowledge.py). The sink records
that alongside its own receive order and flags any discontinuity. This is why
the sequence is assigned at the source: a counter the sink assigned on
receipt would count only what arrived, so a dropped decision would leave no
trace. A producer restart resets its counter, which is reported as a RESTART,
not a GAP — the two look identical if you only look at the numbers, so the
distinction is made explicitly rather than guessed at.

**Hash chaining.** Each record carries `prev_hash`, the `record_hash` of the
previous record FROM THE SAME SOURCE, so each source has its own chain.
Editing or deleting any historical record breaks every chain link after it,
which `verify_chain()` detects. True filesystem append-only (chattr +a) needs
root; a hash chain makes tampering detectable without any privilege at all,
which is the property that actually matters for a submission.

The sink connects as `auditor`, which the ACL grants read on the three decision
topics and write on nothing (mosquitto/acl). It cannot forge the decisions it
records — that separation is the point.

Usage:
    python3 audit/audit_sink.py                    # follow live, append
    python3 audit/audit_sink.py --verify           # check the existing chain
    python3 audit/audit_sink.py --show 20          # last 20 records
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mqtt_identity import apply_credentials

AUDIT_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "audit.jsonl")

TOPICS = [
    ("datacenter/predictions/+", 1),
    ("datacenter/recommendations/+", 1),
    ("datacenter/acks/+", 1),
]

GENESIS = "0" * 64


def _canonical(obj):
    """Stable JSON for hashing — key order and spacing must not vary."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def record_hash(rec):
    """Hash over the fields that constitute the record's claim.

    Deliberately excludes `record_hash` itself and `recv_seq` (a local
    bookkeeping number). Anything included here is protected from silent
    edits; anything excluded is not, so the set is kept to the substance:
    what was said, by whom, when, and what came before.
    """
    material = {
        "prev_hash": rec["prev_hash"],
        "source": rec["source"],
        "topic": rec["topic"],
        "src_seq": rec["src_seq"],
        "received_at": rec["received_at"],
        "payload_sha256": rec["payload_sha256"],
    }
    return hashlib.sha256(_canonical(material).encode()).hexdigest()


class AuditSink:
    def __init__(self, path=AUDIT_PATH):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Per-source state, recovered from the existing file so a restart
        # continues the chain instead of starting a second one.
        self.last_hash = {}
        self.last_src_seq = {}
        self.recv_count = {}
        self._recover()

        # O_APPEND: every write lands at end-of-file atomically. No seeking,
        # no truncation, no rewriting an earlier line — the file only ever
        # grows.
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

    def _recover(self):
        if not os.path.exists(self.path):
            return
        for rec in read_records(self.path):
            src = rec["source"]
            self.last_hash[src] = rec["record_hash"]
            self.last_src_seq[src] = rec["src_seq"]
            self.recv_count[src] = self.recv_count.get(src, 0) + 1
        if self.last_hash:
            print(f"[audit] resuming chains: "
                  + ", ".join(f"{s}@{self.recv_count[s]}" for s in sorted(self.last_hash)))

    def append(self, topic, payload_bytes):
        try:
            payload = json.loads(payload_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A malformed decision message is itself worth recording — it is
            # evidence, not something to discard quietly.
            payload = {"_unparseable": payload_bytes.decode(errors="replace")}

        source = payload.get("source") or _source_from_topic(topic)
        src_seq = payload.get("seq")

        # Hash the CANONICAL form of the parsed payload, not the raw wire
        # bytes. The record stores the payload as a parsed object, so key
        # order is not preserved across a write/read round-trip — hashing
        # raw bytes would make every verification fail on re-serialisation
        # rather than on tampering. Canonical form (sorted keys, fixed
        # separators) is reproducible, and still changes if any value does,
        # which is the property being protected.
        payload_digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()

        note = self._continuity_note(source, src_seq)

        self.recv_count[source] = self.recv_count.get(source, 0) + 1
        rec = {
            "recv_seq": self.recv_count[source],
            "src_seq": src_seq,
            "source": source,
            "topic": topic,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "payload_sha256": payload_digest,
            "prev_hash": self.last_hash.get(source, GENESIS),
            "continuity": note,
            "payload": payload,
        }
        rec["record_hash"] = record_hash(rec)

        os.write(self.fd, (_canonical(rec) + "\n").encode())
        os.fsync(self.fd)

        self.last_hash[source] = rec["record_hash"]
        if src_seq is not None:
            self.last_src_seq[source] = src_seq
        return rec

    def _continuity_note(self, source, src_seq):
        """OK / GAP / RESTART / UNSEQUENCED, decided at write time."""
        if src_seq is None:
            return "UNSEQUENCED"
        prev = self.last_src_seq.get(source)
        if prev is None:
            return "OK"
        if src_seq == prev + 1:
            return "OK"
        if src_seq <= prev:
            # Counters only go forwards. Going backwards means the producer
            # process restarted (its counter reset), which is a different
            # event from losing messages and is labelled as such.
            return f"RESTART(prev={prev},now={src_seq})"
        return f"GAP(missing={src_seq - prev - 1})"


def _source_from_topic(topic):
    """Fallback when a payload carries no `source` field."""
    if "/acks/" in topic:
        return "operator"
    if "/predictions/" in topic or "/recommendations/" in topic:
        return "orchestrator"
    return "unknown"


def read_records(path):
    """Yields parsed records. Skips blank lines; raises on corrupt JSON."""
    with open(path) as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno} is not valid JSON: {e}") from e


def verify_chain(path=AUDIT_PATH):
    """Recomputes every per-source chain.

    Returns (ok, findings). A finding is a human-readable string naming the
    record and what is wrong with it.
    """
    if not os.path.exists(path):
        return True, ["audit log does not exist yet — nothing to verify"]

    findings = []
    expected_prev = {}
    counts = {}

    for i, rec in enumerate(read_records(path), 1):
        src = rec.get("source", "unknown")
        counts[src] = counts.get(src, 0) + 1

        want_prev = expected_prev.get(src, GENESIS)
        if rec.get("prev_hash") != want_prev:
            findings.append(
                f"record {i} ({src}): prev_hash does not match the previous "
                f"record's hash — a record before this one was altered or removed"
            )

        recomputed = record_hash(rec)
        if recomputed != rec.get("record_hash"):
            findings.append(
                f"record {i} ({src}): record_hash mismatch — this record's "
                f"contents were altered after it was written"
            )

        # The payload hash is what ties the chain to the actual message body.
        body = rec.get("payload")
        if body is not None:
            actual = hashlib.sha256(_canonical(body).encode()).hexdigest()
            if actual != rec.get("payload_sha256"):
                findings.append(
                    f"record {i} ({src}): payload does not match payload_sha256 "
                    f"— the recorded decision text was edited"
                )

        expected_prev[src] = rec.get("record_hash")

    return (len(findings) == 0), (findings or
                                  [f"{sum(counts.values())} records across "
                                   f"{len(counts)} sources, all chains intact"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--verify", action="store_true", help="verify the chain and exit")
    ap.add_argument("--show", type=int, metavar="N", help="print the last N records and exit")
    args = ap.parse_args()

    if args.verify:
        ok, findings = verify_chain()
        for f in findings:
            print(("  OK  " if ok else "  !!  ") + f)
        return 0 if ok else 1

    if args.show:
        recs = list(read_records(AUDIT_PATH)) if os.path.exists(AUDIT_PATH) else []
        for r in recs[-args.show:]:
            p = r["payload"]
            extra = p.get("action") or f"p={p.get('failure_probability')}"
            print(f"{r['received_at']}  {r['source']:>12}  seq={r['src_seq']}  "
                  f"{r['continuity']:>18}  {r['topic']:<34} {extra}")
        return 0

    sink = AuditSink()
    use_tls = os.environ.get("MQTT_TLS", "false").lower() == "true"

    client = mqtt.Client(client_id="auditor")
    if use_tls:
        client.tls_set()
    apply_credentials(client, "auditor")

    def on_connect(cl, userdata, flags, rc):
        if rc != 0:
            print(f"[audit] BROKER REFUSED CONNECTION rc={rc} "
                  f"({mqtt.connack_string(rc)}) — nothing will be recorded")
            return
        for topic, qos in TOPICS:
            cl.subscribe(topic, qos=qos)
        print(f"[audit] recording to {sink.path}")

    def on_message(cl, userdata, msg):
        rec = sink.append(msg.topic, msg.payload)
        if rec["continuity"] not in ("OK", "UNSEQUENCED"):
            print(f"[audit] {rec['source']}: {rec['continuity']}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n[audit] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
