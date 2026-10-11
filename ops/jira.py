#!/usr/bin/env python3
"""Jira Cloud client for Driftwood incidents. Standard library only.

Configure with environment variables (put them in ~/.bashrc on the GX10, never in git):
    JIRA_URL        https://yourteam.atlassian.net
    JIRA_EMAIL      the Atlassian account email the token belongs to
    JIRA_TOKEN      API token from https://id.atlassian.com/manage-profile/security/api-tokens
    JIRA_PROJECT    project key, e.g. SRE
  optional:
    JIRA_ISSUE_TYPE   issue type for incidents (default: Task)
    JIRA_STATUS_MAP   how incident stages map onto your workflow's statuses, e.g.
                      "Investigating=In Progress,Awaiting Approval=In Review,Resolved=Done"
                      (default: Open=To Do, Resolved=Done, every other stage=In Progress)
    REQUIRE_APPROVAL  true/false: must a human approve before deploy (default false;
                      `ops/jira.py approval-required on|off` overrides it on this machine)

Incident stages: Open -> Investigating -> Fix Proposed -> Awaiting Approval -> Deploying
-> Monitoring -> Resolved. The exact stage lives in a hidden issue property
("driftwood") and as a dw-<stage> label you can see on the board; the Jira status
follows JIRA_STATUS_MAP.

Human approval: when approval is required, the ticket asks the on-call human to
comment "/approve" or "/reject <reason>" on the issue in Jira. The command gate
calls `wait-approval`, which watches for that comment.

Library use (agents / orchestrator):
    from jira import Jira
    j = Jira()
    key = j.open_incident()                      # reuses an open incident if there is one
    j.comment(key, "Root cause: ...", author="investigator")
    j.move(key, "Fix Proposed", author="engineer")
    j.request_approval(key, "Sandbox: 6.5% -> 0% errors")   # 'pending' or 'not_required'
    j.wait_for_approval(key)                     # True = go, False = rejected
    j.resolve(key, "Index added; healthy for 60s")

CLI (from the repo root):
    python3 ops/jira.py doctor                   # check credentials, project, issue type, statuses
    python3 ops/jira.py open-incident
    python3 ops/jira.py list
    python3 ops/jira.py get KEY
    python3 ops/jira.py comment KEY "text" [--author engineer]   (or --file report.md)
    python3 ops/jira.py move KEY "Investigating" [--author investigator]
    python3 ops/jira.py set KEY root_cause="missing index" fix_branch=fix/INC-7
    python3 ops/jira.py request-approval KEY [--summary "..."]
    python3 ops/jira.py wait-approval KEY [--timeout 900]   exit 0 = go, 1 = rejected, 2 = timed out
    python3 ops/jira.py approve KEY | reject KEY [--reason ...]
    python3 ops/jira.py resolve KEY [--note "..."]
    python3 ops/jira.py approval-required [on|off]
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STAGES = ["Open", "Investigating", "Fix Proposed", "Awaiting Approval", "Deploying", "Monitoring", "Resolved"]
LABEL = "driftwood-incident"
PROP = "driftwood"
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
APPROVAL_OVERRIDE = os.path.join(STATE_DIR, "require_approval")


class JiraError(Exception):
    pass


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {s:02d}s"
    hours, m = divmod(minutes, 60)
    return f"{hours}h {m:02d}m"


def _stage_label(stage: str) -> str:
    return "dw-" + stage.lower().replace(" ", "-")


def _parse_status_map(raw: str) -> dict:
    mapping = {"Open": "To Do", "Resolved": "Done"}
    mapping.update({s: "In Progress" for s in STAGES if s not in mapping})
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        stage, _, status = pair.partition("=")
        if stage.strip() not in STAGES or not status.strip():
            raise JiraError(f"bad JIRA_STATUS_MAP entry '{pair}' (stages: {', '.join(STAGES)})")
        mapping[stage.strip()] = status.strip()
    return mapping


class Jira:
    def __init__(self):
        self.url = os.getenv("JIRA_URL", "").rstrip("/")
        self.project = os.getenv("JIRA_PROJECT", "")
        self.issue_type = os.getenv("JIRA_ISSUE_TYPE", "Task")
        self.status_map = _parse_status_map(os.getenv("JIRA_STATUS_MAP", ""))
        email = os.getenv("JIRA_EMAIL") or os.getenv("JIRA_USER")
        token = os.getenv("JIRA_TOKEN")
        missing = [n for n, v in (("JIRA_URL", self.url), ("JIRA_EMAIL", email), ("JIRA_TOKEN", token),
                                  ("JIRA_PROJECT", self.project)) if not v]
        if missing:
            raise JiraError("not configured: set " + ", ".join(missing) + " (see the top of ops/jira.py)")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    # ------------------------------------------------------------ transport
    def _call(self, method: str, path: str, body=None, params=None):
        url = f"{self.url}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                detail = json.loads(raw)
                msg = "; ".join(detail.get("errorMessages", []) +
                                [f"{k}: {v}" for k, v in (detail.get("errors") or {}).items()])
            except Exception:  # noqa: BLE001
                msg = raw[:200].decode(errors="replace") or e.reason
            hint = {401: " (check JIRA_EMAIL / JIRA_TOKEN)", 403: " (the account lacks permission)",
                    404: " (check JIRA_URL / JIRA_PROJECT / issue key)"}.get(e.code, "")
            raise JiraError(f"{method} {path}: HTTP {e.code} {msg}{hint}") from None
        except urllib.error.URLError as e:
            raise JiraError(f"cannot reach {self.url} ({e.reason})") from None

    # ------------------------------------------------------------ incident state (issue property)
    def _state(self, key: str) -> dict:
        try:
            return self._call("GET", f"/rest/api/2/issue/{key}/properties/{PROP}")["value"]
        except JiraError as e:
            if "HTTP 404" in str(e):
                return {"stage": "Open", "approval": "none", "incident": {}, "history": []}
            raise

    def _save_state(self, key: str, state: dict) -> None:
        self._call("PUT", f"/rest/api/2/issue/{key}/properties/{PROP}", state)

    def _log(self, state: dict, field: str, old, new, author: str) -> None:
        state.setdefault("history", []).append({"ts": _now(), "field": field, "from": old, "to": new, "by": author})

    # ------------------------------------------------------------ issues
    def create(self, summary: str, description: str = "", priority: str | None = "High",
               incident: dict | None = None) -> str:
        fields = {"project": {"key": self.project}, "issuetype": {"name": self.issue_type},
                  "summary": summary[:250], "description": description,
                  "labels": [LABEL, "orders-api", _stage_label("Open")]}
        if priority:
            fields["priority"] = {"name": priority}
        try:
            key = self._call("POST", "/rest/api/2/issue", {"fields": fields})["key"]
        except JiraError as e:
            if "priority" not in str(e).lower():
                raise
            fields.pop("priority")  # priority not on this project's create screen
            key = self._call("POST", "/rest/api/2/issue", {"fields": fields})["key"]
        state = {"stage": "Open", "approval": "none", "incident": incident or {}, "created_ts": _now(), "history": []}
        self._log(state, "stage", None, "Open", "detector")
        self._save_state(key, state)
        return key

    def get(self, key: str) -> dict:
        issue = self._call("GET", f"/rest/api/2/issue/{key}",
                           params={"fields": "summary,status,labels,created,priority,description,comment"})
        issue["driftwood"] = self._state(key)
        return issue

    def search(self, jql: str, max_results: int = 50) -> list[dict]:
        data = self._call("GET", "/rest/api/3/search/jql",
                          params={"jql": jql, "maxResults": max_results,
                                  "fields": "summary,status,labels,created,priority"})
        return data.get("issues", [])

    def open_incidents(self) -> list[dict]:
        """Open Driftwood incidents, oldest first: [{key, summary, stage, jira_status}]."""
        issues = self.search(f'project = "{self.project}" AND labels = "{LABEL}" '
                             f'AND statusCategory != Done ORDER BY created ASC')
        out = []
        for i in issues:
            stage = next((s for s in STAGES if _stage_label(s) in i["fields"].get("labels", [])), "Open")
            if stage == "Resolved":
                continue
            out.append({"key": i["key"], "summary": i["fields"]["summary"], "stage": stage,
                        "jira_status": i["fields"]["status"]["name"]})
        return out

    def comment(self, key: str, body: str, author: str = "agent") -> None:
        self._call("POST", f"/rest/api/2/issue/{key}/comment", {"body": f"*[{author}]* {body}"})

    def _transition_to(self, key: str, status: str) -> None:
        current = self._call("GET", f"/rest/api/2/issue/{key}", params={"fields": "status"})["fields"]["status"]["name"]
        if current.lower() == status.lower():
            return
        options = self._call("GET", f"/rest/api/2/issue/{key}/transitions")["transitions"]
        match = next((t for t in options if t["to"]["name"].lower() == status.lower()), None) or \
            next((t for t in options if t["name"].lower() == status.lower()), None)
        if match is None:
            avail = ", ".join(f"'{t['name']}' -> {t['to']['name']}" for t in options) or "none"
            raise JiraError(f"{key}: no transition from '{current}' to '{status}'. Available: {avail}. "
                            f"Adjust JIRA_STATUS_MAP to match your workflow.")
        self._call("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": match["id"]}})

    def move(self, key: str, stage: str, author: str = "agent") -> None:
        match = next((s for s in STAGES if s.lower() == stage.lower()), None)
        if match is None:
            raise JiraError(f"unknown stage '{stage}' (stages: {', '.join(STAGES)})")
        stage = match
        state = self._state(key)
        if stage == "Deploying" and state.get("approval") in ("pending", "rejected"):
            state["approval"] = self.approval_status(key)  # pick up a fresh /approve comment
            if state["approval"] in ("pending", "rejected"):
                raise JiraError(f"{key}: deploy blocked, approval is {state['approval']}")
            state = self._state(key)
        old = state.get("stage")
        self._transition_to(key, self.status_map[stage])
        labels = [{"remove": _stage_label(s)} for s in STAGES if s != stage] + [{"add": _stage_label(stage)}]
        self._call("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": labels}})
        state["stage"] = stage
        self._log(state, "stage", old, stage, author)
        self._save_state(key, state)

    def set_fields(self, key: str, author: str = "agent", **values) -> None:
        """Record structured incident data (root cause, fix branch, metrics...) on the ticket."""
        state = self._state(key)
        state.setdefault("incident", {}).update(values)
        self._save_state(key, state)
        self.comment(key, "Recorded:\n" + "\n".join(f"* {k}: {v}" for k, v in values.items()), author=author)

    # ------------------------------------------------------------ approval gate
    def approval_required(self) -> bool:
        if os.path.exists(APPROVAL_OVERRIDE):
            return open(APPROVAL_OVERRIDE).read().strip() == "on"
        return os.getenv("REQUIRE_APPROVAL", "false").lower() in ("1", "true", "yes", "on")

    def set_approval_required(self, required: bool) -> bool:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(APPROVAL_OVERRIDE, "w") as f:
            f.write("on" if required else "off")
        return required

    def request_approval(self, key: str, summary: str = "", author: str = "guardian") -> str:
        state = self._state(key)
        if not self.approval_required():
            self._log(state, "approval", state.get("approval"), "not_required", author)
            state["approval"] = "not_required"
            self._save_state(key, state)
            return "not_required"
        # Only /approve or /reject comments posted after this request count.
        existing = self._call("GET", f"/rest/api/2/issue/{key}/comment",
                              params={"orderBy": "created", "maxResults": 1000})["comments"]
        self._log(state, "approval", state.get("approval"), "pending", author)
        state["approval"] = "pending"
        state["approval_after_comment"] = max((int(c["id"]) for c in existing), default=0)
        self._save_state(key, state)
        self.move(key, "Awaiting Approval", author=author)
        self._call("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"add": "dw-needs-approval"}]}})
        self.comment(key, "*Deploy approval needed.*\n" + (summary + "\n\n" if summary else "") +
                     "Reply to this issue with a comment of {{/approve}} to deploy the fix, "
                     "or {{/reject <reason>}} to send it back.", author=author)
        return "pending"

    def approval_status(self, key: str) -> str:
        """none | not_required | pending | approved | rejected. Reads /approve and /reject comments."""
        state = self._state(key)
        if state.get("approval") != "pending":
            return state.get("approval", "none")
        after = state.get("approval_after_comment", 0)
        comments = self._call("GET", f"/rest/api/2/issue/{key}/comment",
                              params={"orderBy": "created", "maxResults": 1000})["comments"]
        for c in comments:
            if int(c["id"]) <= after:
                continue
            m = re.match(r"\s*/(approve|reject)\b\s*(.*)", c["body"] if isinstance(c["body"], str) else "", re.S)
            if not m:
                continue
            decision = "approved" if m.group(1) == "approve" else "rejected"
            who = c.get("author", {}).get("displayName", "human")
            self._log(state, "approval", "pending", decision, who)
            state.update(approval=decision, approval_by=who, approval_reason=m.group(2).strip() or None)
            self._save_state(key, state)
            self._call("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [
                {"remove": "dw-needs-approval"}, {"add": "dw-approved" if decision == "approved" else "dw-rejected"}]}})
            if decision == "rejected":
                self.move(key, "Investigating", author=who)
            return decision
        return "pending"

    def decide(self, key: str, decision: str, reason: str | None = None) -> str:
        """Approve or reject from the command line (posts the same comment a human would)."""
        self._call("POST", f"/rest/api/2/issue/{key}/comment",
                   {"body": ("/approve" if decision == "approved" else "/reject") + (f" {reason}" if reason else "")})
        return self.approval_status(key)

    def wait_for_approval(self, key: str, timeout: float = 900, poll: float = 5) -> bool:
        deadline = _now() + timeout
        while True:
            status = self.approval_status(key)
            if status in ("approved", "not_required"):
                return True
            if status == "rejected":
                return False
            if status == "none":
                raise JiraError(f"{key}: no approval was requested; call request_approval first")
            if _now() >= deadline:
                raise TimeoutError(f"{key}: no approval decision after {timeout:.0f}s")
            time.sleep(poll)

    # ------------------------------------------------------------ lifecycle helpers
    def open_incident(self, snapshot: dict | None = None, prom: str | None = None) -> str:
        """Open an incident for the current SLO breach, or return the key of one already open."""
        existing = self.open_incidents()
        if existing:
            return existing[0]["key"]
        if snapshot is None:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import status  # ops/status.py
            snapshot = status.snapshot(prom or os.getenv("PROM_URL", "http://localhost:9090"))
        breaches = snapshot.get("breaches") or ["manual"]
        lines = [
            f"Detected at {snapshot.get('ts')} on orders-api {snapshot.get('version')} ({snapshot.get('git_sha')}).",
            "",
            f"* Error rate: {snapshot.get('error_rate_pct')} %",
            f"* p95 latency: {snapshot.get('p95_latency_ms')} ms",
            f"* DB pool in use: {snapshot.get('db_pool_utilization_pct')} %",
            f"* Requests/s: {snapshot.get('rps')}",
        ]
        if snapshot.get("slowest_queries_p95_ms"):
            lines.append("* Slowest queries (p95): " +
                         ", ".join(f"{k} {v} ms" for k, v in snapshot["slowest_queries_p95_ms"].items()))
        if snapshot.get("db_errors"):
            lines.append("* DB errors/s: " + ", ".join(f"{k} {v}" for k, v in snapshot["db_errors"].items()))
        lines += ["", "_Opened automatically by the Driftwood detector. Agents post findings as comments._"]
        priority = "Highest" if (snapshot.get("error_rate_pct") or 0) >= 5 else "High"
        return self.create(f"orders-api SLO breach: {', '.join(breaches)} ({snapshot.get('version')})",
                           "\n".join(lines), priority=priority, incident={"detected": snapshot})

    def resolve(self, key: str, note: str = "", author: str = "detector") -> dict:
        """Close with a timeline and time-to-resolve, built from the recorded stage history."""
        state = self._state(key)
        opened = state.get("created_ts") or _now()
        steps = [f"* +{_duration(h['ts'] - opened)}  {h['field']}: {h['from']} -> {h['to']} ({h['by']})"
                 for h in state.get("history", []) if h["from"] is not None]
        mttr = _now() - opened
        body = "\n".join(([note, ""] if note else []) + ["*Timeline*"] + steps +
                         [f"* +{_duration(mttr)}  resolved", "", f"*Time to resolve: {_duration(mttr)}*"])
        self.comment(key, body, author=author)
        state = self._state(key)
        state.setdefault("incident", {}).update(mttr_seconds=round(mttr), resolution_note=note or None)
        self._save_state(key, state)
        self.move(key, "Resolved", author=author)
        return {"key": key, "mttr_seconds": round(mttr), "mttr": _duration(mttr)}

    def doctor(self) -> list[str]:
        """Check the configuration against the live Jira site. Returns a list of findings."""
        out = []
        me = self._call("GET", "/rest/api/2/myself")
        out.append(f"ok  authenticated as {me.get('displayName')} ({me.get('emailAddress', 'email hidden')})")
        proj = self._call("GET", f"/rest/api/2/project/{self.project}")
        out.append(f"ok  project {proj['key']}: {proj['name']}")
        statuses = self._call("GET", f"/rest/api/2/project/{self.project}/statuses")
        types = {t["name"]: {s["name"] for s in t["statuses"]} for t in statuses}
        if self.issue_type not in types:
            out.append(f"ERR issue type '{self.issue_type}' not in project; set JIRA_ISSUE_TYPE to one of: "
                       + ", ".join(types))
            return out
        out.append(f"ok  issue type '{self.issue_type}'; workflow statuses: " + ", ".join(sorted(types[self.issue_type])))
        for stage in STAGES:
            target = self.status_map[stage]
            mark = "ok " if target in types[self.issue_type] else "ERR"
            out.append(f"{mark} stage {stage:<18} -> status '{target}'")
        if any(line.startswith("ERR") for line in out):
            out.append("    fix: set JIRA_STATUS_MAP, e.g. "
                       "export JIRA_STATUS_MAP='Investigating=In Progress,Resolved=Done'")
        out.append(f"ok  human approval required: {'on' if self.approval_required() else 'off'}")
        return out


# ------------------------------------------------------------------ CLI
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("open-incident").add_argument("--prom", default=None)
    sub.add_parser("list")
    g = sub.add_parser("get")
    g.add_argument("key")
    g.add_argument("--json", action="store_true")
    c = sub.add_parser("comment")
    c.add_argument("key")
    c.add_argument("text", nargs="?")
    c.add_argument("--file")
    c.add_argument("--author", default="agent")
    m = sub.add_parser("move")
    m.add_argument("key")
    m.add_argument("stage", help=", ".join(STAGES))
    m.add_argument("--author", default="agent")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("pairs", nargs="+", help="name=value")
    s.add_argument("--author", default="agent")
    r = sub.add_parser("request-approval")
    r.add_argument("key")
    r.add_argument("--summary", default="")
    w = sub.add_parser("wait-approval")
    w.add_argument("key")
    w.add_argument("--timeout", type=float, default=900)
    for name in ("approve", "reject"):
        d = sub.add_parser(name)
        d.add_argument("key")
        d.add_argument("--reason")
    z = sub.add_parser("resolve")
    z.add_argument("key")
    z.add_argument("--note", default="")
    a = sub.add_parser("approval-required")
    a.add_argument("value", nargs="?", choices=["on", "off"])
    args = p.parse_args()

    try:
        j = Jira()
        if args.cmd == "doctor":
            findings = j.doctor()
            print("\n".join(findings))
            return 1 if any(f.startswith("ERR") for f in findings) else 0
        if args.cmd == "open-incident":
            print(j.open_incident(prom=args.prom))
        elif args.cmd == "list":
            for i in j.open_incidents():
                print(f"{i['key']:<10} {i['stage']:<18} ({i['jira_status']})  {i['summary']}")
        elif args.cmd == "get":
            issue = j.get(args.key)
            if args.json:
                print(json.dumps(issue, indent=2))
            else:
                f, st = issue["fields"], issue["driftwood"]
                print(f"{issue['key']}  stage={st.get('stage')}  jira={f['status']['name']}  "
                      f"approval={st.get('approval')}\n  {f['summary']}\n  {j.url}/browse/{issue['key']}")
                if st.get("incident"):
                    print("  incident: " + json.dumps({k: v for k, v in st["incident"].items() if k != "detected"}))
        elif args.cmd == "comment":
            text = open(args.file).read() if args.file else args.text
            if not text:
                p.error("comment needs text or --file")
            j.comment(args.key, text, author=args.author)
        elif args.cmd == "move":
            j.move(args.key, args.stage, author=args.author)
        elif args.cmd == "set":
            j.set_fields(args.key, author=args.author,
                         **{k: v for k, _, v in (pair.partition("=") for pair in args.pairs)})
        elif args.cmd == "request-approval":
            print(j.request_approval(args.key, args.summary))
        elif args.cmd == "wait-approval":
            try:
                ok = j.wait_for_approval(args.key, timeout=args.timeout)
            except TimeoutError as e:
                print(e, file=sys.stderr)
                return 2
            print("approved" if ok else "rejected")
            return 0 if ok else 1
        elif args.cmd in ("approve", "reject"):
            print(j.decide(args.key, "approved" if args.cmd == "approve" else "rejected", args.reason))
        elif args.cmd == "resolve":
            print(json.dumps(j.resolve(args.key, args.note)))
        elif args.cmd == "approval-required":
            print("on" if (j.set_approval_required(args.value == "on") if args.value else j.approval_required())
                  else "off")
    except JiraError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
