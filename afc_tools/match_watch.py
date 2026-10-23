"""READ-ONLY match monitor: what is the match server actually doing with our five runtimes?

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\match_watch.py                 # last 15 minutes
    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\match_watch.py --minutes 60

Never invokes anything: it only reads the runtimes' CloudWatch log groups
(logs:FilterLogEvents) and the stack outputs (cloudformation:DescribeStacks).
Built for match day, when nobody can tell us how the server calls the team. Per runtime:
  calls         handler lines "agent invoked for team T, controlling player P"
  sessions      distinct runtimeSessionIds. calls >> sessions = the server reuses sessions
                (warm, ~1 s ticks measured on 2026-10-18); calls == sessions = a new session per
                call (cold, ~7-9 s each)
  team/player   what the server asked for: must be our team id and this runtime's player
  interval      median time between consecutive calls in the same session (the tick period)
  handler       median time from "agent invoked" to the decision line (our code + the model)
  sources       rule / model / validator fallback / last resort / error path
  captures      LAB_CAPTURE lines (only the runtimes with AFC_CAPTURE_STATE=1)
  protocol      AFC_PROTOCOL warnings: payloads without {"prompt": {gameState, teamId, myPlayers}}
                (key names only), i.e. the server speaks a different envelope than the sample
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict

import boto3
from botocore.exceptions import ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from e2e_invoke import AGENTS, err, runtime_arns  # noqa: E402

INVOKED = re.compile(r"agent invoked for team (\S+), controlling player (\S+)")
DECISIONS = [("answered by rule", "rule"), ("LLM returned", "model"), ("rejected by validator", "validator_fallback"),
             ("Fallback output failed validation", "last_resort"), ("agent error", "error_fallback")]


def handler_lines(logs, group, start_ms, max_pages):
    """The handler's JSON log lines (they carry sessionId), oldest first."""
    out, token, pages = [], None, 0
    while pages < max_pages:
        kw = {"logGroupName": group, "startTime": start_ms}
        if token:
            kw["nextToken"] = token
        r = logs.filter_log_events(**kw)
        for ev in r["events"]:
            try:
                j = json.loads(ev["message"])
            except ValueError:
                continue
            if isinstance(j, dict) and j.get("sessionId"):
                out.append((ev["timestamp"], j["sessionId"], str(j.get("message", ""))))
        token, pages = r.get("nextToken"), pages + 1
        if not token:
            break
    return sorted(out), bool(token)


def median(values):
    return f"{statistics.median(values):.2f}s" if values else "-"


def summarize(lines):
    calls, sessions, asked = [], set(), Counter()
    sources, captures, handler_s = Counter(), 0, []
    protocol = []
    by_session = defaultdict(list)
    open_call = {}
    for ts, sid, msg in lines:
        if msg.startswith("LAB_CAPTURE "):
            captures += 1
            continue
        if msg.startswith("AFC_PROTOCOL "):
            protocol.append(msg)
            continue
        m = INVOKED.search(msg)
        if m:
            calls.append(ts)
            sessions.add(sid)
            asked[(m.group(1), m.group(2))] += 1
            by_session[sid].append(ts)
            open_call[sid] = ts
            continue
        hit = next((s for marker, s in DECISIONS if marker in msg), None)
        if hit:
            sources[hit] += 1
            if sid in open_call:
                handler_s.append((ts - open_call.pop(sid)) / 1000)
    gaps = [(b - a) / 1000 for ts_list in by_session.values() for a, b in zip(ts_list, ts_list[1:])]
    return {"calls": len(calls), "sessions": len(sessions), "asked": dict(asked), "sources": dict(sources),
            "protocolWarnings": len(protocol), "protocolSample": protocol[0][:300] if protocol else None,
            "captures": captures, "handlerMedian": median(handler_s), "intervalMedian": median(gaps),
            "first": min(calls) if calls else None, "last": max(calls) if calls else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--team", type=int, help="the team id we expect the server to send (0 or 1), if known")
    ap.add_argument("--max-pages", type=int, default=50, help="FilterLogEvents pages per runtime")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    logs = session.client("logs")
    arns = runtime_arns(session.client("cloudformation"))
    start_ms = int((time.time() - args.minutes * 60) * 1000)
    print(f"last {args.minutes:g} min, region {session.region_name} (read-only)\n")
    totals = Counter()
    for key, _, pid in AGENTS:
        group = f"/aws/bedrock-agentcore/runtimes/{arns[key].split('/')[-1]}-DEFAULT"
        try:
            lines, truncated = handler_lines(logs, group, start_ms, args.max_pages)
        except ClientError as e:
            print(f"{key:5s} logs unavailable: {err(e)}")
            continue
        s = summarize(lines)
        totals.update(calls=s["calls"], sessions=s["sessions"])
        reuse = ("-" if not s["calls"] else
                 "new session per call (cold)" if s["calls"] == s["sessions"] else
                 f"reused ({s['calls'] / s['sessions']:.1f} calls/session)")
        wrong = {k: v for k, v in s["asked"].items()
                 if k[1] != str(pid) or (args.team is not None and k[0] != str(args.team))}
        print(f"{key:5s} calls {s['calls']:4d} | sessions {s['sessions']:3d} {reuse}")
        print(f"      asked (team, player): {s['asked'] or '-'}" + (f"   <-- UNEXPECTED {wrong}" if wrong else ""))
        print(f"      interval {s['intervalMedian']} | handler {s['handlerMedian']} | sources {s['sources'] or '-'}"
              f" | captures {s['captures']}" + (" | (log pages truncated)" if truncated else ""))
        if s["protocolWarnings"]:
            print(f"      <-- PROTOCOL: {s['protocolWarnings']} payloads without the expected shape, e.g. {s['protocolSample']}")
        if s["sources"].get("error_fallback") or s["sources"].get("last_resort"):
            print("      <-- errors: read this runtime's log group")
    if totals["calls"]:
        print(f"\nteam: {totals['calls']} calls in {totals['sessions']} sessions")
    else:
        print("\nno handler calls in this window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
