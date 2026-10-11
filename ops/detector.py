#!/usr/bin/env python3
"""Deterministic incident detector: watches the SLOs and drives tickets. No LLM involved.

    python3 ops/detector.py                 # run until Ctrl+C
    python3 ops/detector.py --once          # one check, then exit

Every --interval seconds it takes the same snapshot as ops/status.py.
  * --strikes consecutive breaches with no open incident  -> opens an incident ticket
  * an open incident in "Deploying" whose metrics are healthy -> moves it to "Monitoring"
  * --recover consecutive healthy checks while "Monitoring"   -> resolves it with timeline and MTTR
    (disable with --no-auto-resolve if your agents resolve tickets themselves)

Each event is printed as one JSON line and appended to state/detector.log, so the
orchestrator can either tail that file or poll `python3 ops/jira.py list`.
Needs the JIRA_* settings described at the top of ops/jira.py.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import status  # noqa: E402  ops/status.py
from jira import Jira, JiraError  # noqa: E402  ops/jira.py (Jira Cloud)

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "detector.log")


def emit(event: str, **fields) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields})
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prom", default=os.getenv("PROM_URL", "http://localhost:9090"))
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--strikes", type=int, default=3, help="consecutive breaches before opening a ticket")
    p.add_argument("--recover", type=int, default=12, help="consecutive healthy checks before auto-resolving")
    p.add_argument("--no-auto-resolve", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    try:
        jira = Jira()
    except JiraError as e:
        sys.exit(f"detector: {e}")
    strikes = healthy = 0
    emit("detector_started", prom=args.prom, tracker=jira.url, strikes=args.strikes, recover=args.recover)
    while True:
        try:
            snap = status.snapshot(args.prom)
        except Exception as e:  # noqa: BLE001 - prometheus down or starting
            emit("metrics_unavailable", error=str(e)[:200])
            snap = None

        if snap is not None:
            strikes = 0 if snap["healthy"] else strikes + 1
            healthy = healthy + 1 if snap["healthy"] else 0
            try:
                open_issues = jira.open_incidents()
                current = open_issues[0] if open_issues else None
                state = current["stage"] if current else None

                if current is None and strikes >= args.strikes:
                    key = jira.open_incident(snapshot=snap)
                    emit("incident_opened", key=key, breaches=snap["breaches"], version=snap["version"],
                         error_rate_pct=snap["error_rate_pct"], p95_latency_ms=snap["p95_latency_ms"])
                elif current and not args.no_auto_resolve:
                    key = current["key"]
                    if state == "Deploying" and snap["healthy"]:
                        jira.move(key, "Monitoring", author="detector")
                        jira.comment(key, f"Metrics healthy after deploy of {snap['version']} ({snap['git_sha']}): "
                                          f"error rate {snap['error_rate_pct']} %, p95 {snap['p95_latency_ms']} ms. "
                                          f"Watching for {args.recover * args.interval:.0f}s before resolving.",
                                     author="detector")
                        emit("incident_monitoring", key=key, version=snap["version"])
                    elif state == "Monitoring" and not snap["healthy"]:
                        jira.move(key, "Investigating", author="detector")
                        jira.comment(key, f"Regression while monitoring: {', '.join(snap['breaches'])}. "
                                          "Back to investigation.", author="detector")
                        emit("incident_regressed", key=key, breaches=snap["breaches"])
                    elif state == "Monitoring" and healthy >= args.recover:
                        result = jira.resolve(key, note=f"Healthy for {healthy * args.interval:.0f}s on "
                                                        f"{snap['version']} ({snap['git_sha']}).")
                        emit("incident_resolved", **result)
            except JiraError as e:
                emit("tracker_unavailable", error=str(e)[:200])

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
