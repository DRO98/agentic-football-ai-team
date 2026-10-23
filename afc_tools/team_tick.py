"""Team integration check: for each scenario tick, invoke all five deployed runtimes at once (as the
match server would) and judge the team's combined answer.

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\team_tick.py                # plan: scenarios + expected possession, no paid calls
    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\team_tick.py --invoke       # PAID: 5 invocations per scenario

Scenarios are built from afc_tools/sample_prompt.txt (test data, not a real capture), so they share its
assumptions: both teams use agentId_0..4, coordinates are not mirrored for the away team.
Per tick:
  - every reply must be exactly one valid command for that runtime's player and team (lib/action_validator.py)
  - team checks (warnings, not failures): more than one on-ball command; three or more players
    sent to the ball; GK leaving its own box (x > -36 for team 0)
  - source of each reply (rule / model / validator fallback / error path), from the runtime's JSON log
    lines matched by this call's runtimeSessionId
  - the slowest runtime's latency, which bounds the tick rate
  - with --reuse-sessions: one runtimeSessionId per agent for every tick, so cold (first call)
    and warm (later calls) latency can be compared
Report: afc_tools/reports/team-<time>.json. Exit 0 only if every reply was valid.
"""

import argparse
import concurrent.futures as cf
import copy
import json
import os
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)
from action_validator import context_from_state, extract_reply, validate_reply  # noqa: E402
from e2e_invoke import AGENTS, decode_reply, err, runtime_arns  # noqa: E402
from possession import describe, resolve_possession  # noqa: E402

ON_BALL = {"PASS", "SHOOT", "GK_DISTRIBUTE"}
TO_BALL = {"PRESS_BALL", "INTERCEPT"}
GK_BOX_X = -36.0  # team 0 defends x = -55; the box edge is an assumption, used only for a warning


def _player(state, team_code, idx):
    return [p for p in state["players"] if p["teamCode"] == team_code][idx]


def scenarios(base):
    """(name, team_id, prompt_data). Each edit keeps the state physically consistent for lib/possession.py."""
    out = [("fwd1_on_ball_attack", 0, copy.deepcopy(base))]

    s = copy.deepcopy(base)  # our GK holds the ball in its own box
    s["gameState"]["ball"].update(position={"x": -50, "y": 0, "z": 0}, isFree=False, possessionAgentId="agentId_0")
    out.append(("gk_holds_ball", 0, s))

    s = copy.deepcopy(base)  # loose ball rolling into our box
    s["gameState"]["ball"].update(position={"x": -42, "y": 4, "z": 0}, velocity={"x": -3, "y": 0, "z": 0},
                                  isFree=True, possessionAgentId=None)
    out.append(("loose_ball_our_box", 0, s))

    s = copy.deepcopy(base)  # away #3 dribbles at our goal
    _player(s["gameState"], "away", 3)["position"] = {"x": -28, "y": -6}
    s["gameState"]["ball"].update(position={"x": -27.5, "y": -6, "z": 0}, isFree=False, possessionAgentId="agentId_3")
    out.append(("opponent_attack", 0, s))

    out.append(("sample_as_away_team", 1, copy.deepcopy(base)))  # same state, we are team 1
    return out


def expected(prompt_data, team_id):
    gs = prompt_data["gameState"]
    return describe(resolve_possession(gs["ball"], gs["players"], team_id))


def new_session_id():
    return f"afc-team-{uuid.uuid4().hex}"


def invoke_one(client, arn, prompt_data, pid, team_id, sid=None):
    data = {**prompt_data, "teamId": team_id, "myPlayers": [pid]}
    payload = json.dumps({"prompt": json.dumps(data, separators=(",", ":"))}).encode()
    sid = sid or new_session_id()
    t0 = time.perf_counter()
    try:
        r = client.invoke_agent_runtime(agentRuntimeArn=arn, qualifier="DEFAULT", runtimeSessionId=sid,
                                        contentType="application/json", accept="application/json, text/event-stream",
                                        payload=payload)
        reply = decode_reply(r["response"].read(), r.get("contentType"))
        return {"session": sid, "latencyS": round(time.perf_counter() - t0, 2), "reply": reply[:2000]}
    except (ClientError, BotoCoreError) as e:
        return {"session": sid, "latencyS": round(time.perf_counter() - t0, 2), "error": err(e)}


def team_checks(prompt_data, team_id, cmds):
    warnings = []
    ball = prompt_data["gameState"]["ball"]["position"]
    on_ball = [c for c in cmds if c["commandType"] in ON_BALL]
    if len(on_ball) > 1:
        warnings.append(f"{len(on_ball)} on-ball commands: {[(c['playerId'], c['commandType']) for c in on_ball]}")
    rush = [c["playerId"] for c in cmds if c["commandType"] in TO_BALL or (
        c["commandType"] == "MOVE_TO" and abs(c["parameters"]["target_x"] - ball["x"]) <= 5
        and abs(c["parameters"]["target_y"] - ball["y"]) <= 5)]
    if len(rush) >= 3:
        warnings.append(f"{len(rush)} players sent to the ball: {rush}")
    gk = [c for c in cmds if c["playerId"] == 0 and c["commandType"] == "MOVE_TO"]
    if team_id == 0 and gk and gk[0]["parameters"]["target_x"] > GK_BOX_X:
        warnings.append(f"GK MOVE_TO x={gk[0]['parameters']['target_x']} is outside its box (x > {GK_BOX_X})")
    return warnings


def attribute(logs, rows, start_ms, wait_s):
    """Match each call to its handler line in that runtime's JSON logs, by runtimeSessionId.

    With --reuse-sessions several calls share a session; a runtime answers one tick at a time
    and the ticks are sequential, so the k-th handler line of a session (by timestamp) belongs
    to the k-th call made in it (rows are in call order).
    """
    markers = [("answered by rule", "rule"), ("LLM returned", "model"), ("rejected by validator", "validator_fallback"),
               ("Fallback output failed validation", "last_resort"), ("agent error", "error_fallback")]
    calls = {}
    for r in rows:
        if "session" in r:
            calls.setdefault(r["session"], []).append(r)
    deadline = time.time() + wait_s
    while True:
        found = {s: [] for s in calls}
        captures = {s: 0 for s in calls}
        for group in {r["logGroup"] for rs in calls.values() for r in rs}:
            try:
                events = logs.filter_log_events(logGroupName=group, startTime=start_ms - 2000)["events"]
            except ClientError as e:
                for rs in calls.values():
                    for r in rs:
                        r["source"] = f"logs_unavailable: {err(e)}"
                return
            for ev in sorted(events, key=lambda e: e["timestamp"]):
                try:
                    j = json.loads(ev["message"])
                except ValueError:
                    continue
                if not isinstance(j, dict) or j.get("sessionId") not in calls:
                    continue
                msg = str(j.get("message", ""))
                if msg.startswith("LAB_CAPTURE "):
                    captures[j["sessionId"]] += 1
                hit = next((source for marker, source in markers if marker in msg), None)
                if hit:
                    found[j["sessionId"]].append((hit, msg[:300]))
        if all(len(found[s]) >= len(rs) for s, rs in calls.items()) or time.time() >= deadline:
            break
        time.sleep(5)
    for s, rs in calls.items():
        for k, r in enumerate(rs):
            r["source"], r["logLine"] = found[s][k] if k < len(found[s]) else ("not_found_in_logs", "")
            r["captured"] = k < captures[s]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--only", action="append", help="limit to these scenario names")
    ap.add_argument("--invoke", action="store_true", help="PAID: invoke the five runtimes per scenario")
    ap.add_argument("--log-wait", type=int, default=90)
    ap.add_argument("--reuse-sessions", action="store_true",
                    help="one runtimeSessionId per agent for all ticks (first tick cold, the rest warm)")
    args = ap.parse_args()

    with open(os.path.join(HERE, "sample_prompt.txt"), encoding="utf-8") as f:
        base = json.load(f)
    plan = [s for s in scenarios(base) if not args.only or s[0] in args.only]
    for name, team_id, data in plan:
        print(f"  scenario {name:22s} team {team_id} | possession: {expected(data, team_id)}")
    if not args.invoke:
        print(f"Plan only: {len(plan)} scenarios x 5 runtimes = {len(plan) * 5} paid invocations with --invoke.")
        return 0

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    print("identity:", session.client("sts").get_caller_identity()["Arn"])
    arns = runtime_arns(session.client("cloudformation"))
    client = session.client("bedrock-agentcore", config=Config(read_timeout=180, retries={"max_attempts": 1},
                                                                max_pool_connections=10))
    start_ms = int(time.time() * 1000)
    report = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "region": session.region_name,
              "reuseSessions": args.reuse_sessions, "ticks": []}
    sessions = {key: new_session_id() for key, _, _ in AGENTS} if args.reuse_sessions else {}
    all_rows = []
    for tick_no, (name, team_id, data) in enumerate(plan):
        with cf.ThreadPoolExecutor(max_workers=5) as pool:
            futs = {key: pool.submit(invoke_one, client, arns[key], data, pid, team_id, sessions.get(key))
                    for key, _, pid in AGENTS}
        rows, cmds = [], []
        for key, _, pid in AGENTS:
            row = {"agent": key, "player": pid, **futs[key].result(),
                   "warm": args.reuse_sessions and tick_no > 0,
                   "logGroup": f"/aws/bedrock-agentcore/runtimes/{arns[key].split('/')[-1]}-DEFAULT"}
            if "reply" in row:
                v = validate_reply(extract_reply(row["reply"]), context_from_state(data["gameState"], team_id, pid))
                row.update(valid=v.ok, reason=v.reason, issues=[str(i) for i in v.issues])
                cmds += list(v.commands) if v.ok else []
            else:
                row["valid"] = False
            rows.append(row)
        tick = {"scenario": name, "team": team_id, "possession": expected(data, team_id), "agents": rows,
                "maxLatencyS": max(r["latencyS"] for r in rows), "teamWarnings": team_checks(data, team_id, cmds)}
        report["ticks"].append(tick)
        all_rows += rows

    attribute(session.client("logs"), all_rows, start_ms, args.log_wait)
    invalid = 0
    for t in report["ticks"]:
        print(f"\n== {t['scenario']} (team {t['team']}) possession: {t['possession']} | slowest {t['maxLatencyS']}s")
        for r in t["agents"]:
            invalid += not r["valid"]
            shown = r.get("reply") or r.get("error")
            cap = " +capture" if r.get("captured") else ""
            print(f"   {r['agent']:5s} valid={r['valid']!s:5s} {r.get('source', '?'):18s} {r['latencyS']:5.2f}s{cap} {shown[:150]}")
            if r.get("issues"):
                print(f"         issues: {r['issues']}")
        for w in t["teamWarnings"]:
            print(f"   team warning: {w}")
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    out = os.path.join(HERE, "reports", f"team-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    if args.reuse_sessions:
        print("\nlatency per agent (s): first call in the session (cold) | later calls (warm)")
        for key, _, _ in AGENTS:
            cold = [r["latencyS"] for r in all_rows if r["agent"] == key and not r["warm"]]
            warm = [r["latencyS"] for r in all_rows if r["agent"] == key and r["warm"]]
            src = [r.get("source", "?")[0] for r in all_rows if r["agent"] == key]
            print(f"   {key:5s} cold {cold} | warm {warm} | sources {''.join(src)}")
        print("   ticks (slowest of five):", [t["maxLatencyS"] for t in report["ticks"]])
    n = len(all_rows)
    print(f"\n{n - invalid}/{n} replies valid | report: {os.path.relpath(out, ROOT)}")
    return 0 if invalid == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
