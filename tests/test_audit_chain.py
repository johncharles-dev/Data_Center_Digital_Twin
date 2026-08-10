"""Verifies the audit trail's two guarantees, by breaking them.

A hash chain that has never been tested against an actual edit is a claim,
not a guarantee. Each case here writes a real audit file, tampers with it the
way someone covering their tracks would, and asserts the verifier says so.

    python3 tests/test_audit_chain.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audit.audit_sink import AuditSink, verify_chain, read_records

PASS, FAIL = "PASS", "FAIL"
failures = []


def check(label, condition, detail=""):
    print(f"  {PASS if condition else FAIL}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)


_case = [0]


def make_log(tmpdir, events):
    """Writes an audit file from (topic, payload dict) pairs.

    A fresh filename per call: AuditSink resumes an existing chain by design,
    so reusing one path would accumulate every case's records into the next.
    """
    _case[0] += 1
    path = os.path.join(tmpdir, f"audit-{_case[0]}.jsonl")
    sink = AuditSink(path)
    for topic, payload in events:
        sink.append(topic, json.dumps(payload).encode())
    os.close(sink.fd)
    return path


def orch(seq, prob):
    return ("datacenter/predictions/CRAC-01",
            {"source": "orchestrator", "seq": seq, "failure_probability": prob})


def main():
    tmp = tempfile.mkdtemp(prefix="audit-test-")
    try:
        # ---------------------------------------------------------- clean log
        print("\n1. A clean chain verifies:")
        path = make_log(tmp, [orch(i, 0.1 * i) for i in range(1, 6)])
        ok, findings = verify_chain(path)
        check("5 records, chain intact", ok, findings[0])

        recs = list(read_records(path))
        check("all continuity notes are OK",
              all(r["continuity"] == "OK" for r in recs),
              ",".join(r["continuity"] for r in recs))
        check("first record chains to genesis",
              recs[0]["prev_hash"] == "0" * 64)
        check("each record chains to the previous",
              all(recs[i]["prev_hash"] == recs[i - 1]["record_hash"]
                  for i in range(1, len(recs))))

        # ------------------------------------------------------ gap detection
        print("\n2. A dropped decision is flagged as a GAP:")
        path = make_log(tmp, [orch(1, 0.1), orch(2, 0.2), orch(5, 0.5)])
        recs = list(read_records(path))
        check("seq 3 and 4 missing -> GAP(missing=2)",
              recs[2]["continuity"] == "GAP(missing=2)",
              recs[2]["continuity"])
        ok, _ = verify_chain(path)
        check("a gap does NOT break the hash chain", ok,
              "the chain records what arrived; the gap note records what didn't")

        # -------------------------------------------------- restart detection
        print("\n3. A producer restart is distinguished from a gap:")
        path = make_log(tmp, [orch(7, 0.7), orch(8, 0.8), orch(1, 0.1)])
        recs = list(read_records(path))
        check("counter going backwards -> RESTART, not GAP",
              recs[2]["continuity"].startswith("RESTART"),
              recs[2]["continuity"])

        # ---------------------------------------------- per-source separation
        print("\n4. Sources keep independent chains and counters:")
        path = make_log(tmp, [
            orch(1, 0.1),
            ("datacenter/acks/room", {"source": "operator", "seq": 1, "action": "acknowledged"}),
            orch(2, 0.2),
            ("datacenter/acks/room", {"source": "operator", "seq": 2, "action": "rejected"}),
        ])
        recs = list(read_records(path))
        check("interleaved sources all report OK",
              all(r["continuity"] == "OK" for r in recs),
              ",".join(f"{r['source']}:{r['continuity']}" for r in recs))
        check("operator's chain links to operator, not orchestrator",
              recs[3]["prev_hash"] == recs[1]["record_hash"])
        ok, _ = verify_chain(path)
        check("both chains verify", ok)

        # ------------------------------------------------- tamper: edit a body
        print("\n5. Editing a recorded decision is detected:")
        path = make_log(tmp, [orch(i, 0.1 * i) for i in range(1, 6)])
        lines = open(path).read().splitlines()
        rec = json.loads(lines[2])
        rec["payload"]["failure_probability"] = 0.99   # rewrite history
        lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        open(path, "w").write("\n".join(lines) + "\n")
        ok, findings = verify_chain(path)
        check("altered payload -> verification fails", not ok)
        check("finding names the payload mismatch",
              any("payload_sha256" in f or "decision text" in f for f in findings),
              findings[0] if findings else "no findings")

        # ----------------------------------------------- tamper: delete a line
        print("\n6. Deleting a record is detected:")
        path = make_log(tmp, [orch(i, 0.1 * i) for i in range(1, 6)])
        lines = open(path).read().splitlines()
        del lines[2]                                    # make it never happened
        open(path, "w").write("\n".join(lines) + "\n")
        ok, findings = verify_chain(path)
        check("removed record -> chain break detected", not ok)
        check("finding names the broken link",
              any("prev_hash" in f for f in findings),
              findings[0] if findings else "no findings")

        # --------------------------------------------------- restart recovery
        print("\n7. A sink restart continues the chain rather than forking it:")
        path = os.path.join(tmp, "resume.jsonl")
        s1 = AuditSink(path)
        for i in range(1, 4):
            s1.append(*[orch(i, 0.1 * i)[0], json.dumps(orch(i, 0.1 * i)[1]).encode()])
        os.close(s1.fd)
        s2 = AuditSink(path)                            # simulate a restart
        s2.append("datacenter/predictions/CRAC-01",
                  json.dumps({"source": "orchestrator", "seq": 4,
                              "failure_probability": 0.4}).encode())
        os.close(s2.fd)
        recs = list(read_records(path))
        check("record after restart chains to the last pre-restart record",
              recs[3]["prev_hash"] == recs[2]["record_hash"])
        check("continuity across the restart is OK",
              recs[3]["continuity"] == "OK", recs[3]["continuity"])
        ok, _ = verify_chain(path)
        check("resumed chain verifies end to end", ok)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} assertion(s) failed: {failures}")
        return 1
    print("All audit-chain assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
