"""End-to-end check of the deployed team: invoke each AgentCore runtime once and validate its reply.

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\e2e_invoke.py                 # plan only, no calls that cost
    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\e2e_invoke.py --invoke        # PAID: one invocation per runtime

Per runtime (GK, DEF, MID, FWD1, FWD2):
  1. resolve      the runtime ARN from the stack outputs (cloudformation:DescribeStacks)
  2. payload      {"prompt": <afc_tools/sample_prompt.txt with myPlayers=[that player]>}, as the server sends it
  3. invoke       bedrock-agentcore:InvokeAgentRuntime, once (no SDK retries: a retry is another paid call)
  4. validate     the reply with lib/action_validator.py for that player and team: exactly one
                  command, schema, ids, pitch bounds, possession
  5. attribute    the runtime's CloudWatch log lines for this call: was it the model ("LLM returned"),
                  the validator's fallback ("rejected by validator"), or the error path ("agent error")?
A report is written to afc_tools/reports/e2e-<time>.json. Exit code 0 only if every runtime
answered with a valid command for its own player. Credentials are never printed.
"""

import argparse
import hashlib
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
from action_validator import context_from_state, extract_reply, validate_reply  # noqa: E402
from frame import SERVER_HALF_LENGTH, SERVER_HALF_WIDTH, to_design_frame  # noqa: E402

STACK = "AgentCore-TeamStrandsBalanced-default"
AGENTS = [("gk", "Gk", 0), ("def", "Def", 1), ("mid", "Mid", 2), ("fwd1", "Fwd1", 3), ("fwd2", "Fwd2", 4)]
LOG_MARKERS = [("answered by rule", "rule"), ("LLM returned", "model"), ("rejected by validator", "validator_fallback"),
               ("Fallback output failed validation", "last_resort"), ("agent error", "error_fallback")]


def err(e):
    if isinstance(e, ClientError):
        return f"{e.response['Error'].get('Code')}: {e.response['Error'].get('Message', '')[:200]}"
    return f"{type(e).__name__}: {str(e)[:200]}"


def runtime_arns(cfn):
    outs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
    arns = {}
    for key, camel, _ in AGENTS:
        match = [v for k, v in outs.items() if k.startswith(f"ApplicationAgentAi{camel}AgentRuntimeArnOutput")]
        arns[key] = match[0] if match else None
    return arns


def decode_reply(body: bytes, content_type: str) -> str:
    """The entrypoint yields json.dumps(commands); AgentCore streams it (SSE) or returns it as JSON."""
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or ""):
        parts = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        chunks = []
        for p in parts:
            try:
                v = json.loads(p)
            except ValueError:
                v = p
            chunks.append(v if isinstance(v, str) else json.dumps(v))
        return "".join(chunks)
    try:
        v = json.loads(text)
        return v if isinstance(v, str) else json.dumps(v)
    except ValueError:
        return text


def attribute(logs, runtime_id, start_ms, wait_s):
    """Find this call's handler lines in the runtime's log group (ingestion takes a few seconds)."""
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    deadline = time.time() + wait_s
    lines = []
    while time.time() < deadline:
        try:
            events = logs.filter_log_events(logGroupName=group, startTime=start_ms - 2000)["events"]
        except ClientError as e:
            return "logs_unavailable", [err(e)]
        lines = [ev["message"].strip() for ev in events]
        for marker, source in LOG_MARKERS:
            hits = [m for m in lines if marker in m]
            if hits:
                return source, hits[-3:]
        time.sleep(5)
    return "not_found_in_logs", lines[-5:]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--only", choices=[a[0] for a in AGENTS], action="append", help="limit to these runtimes")
    ap.add_argument("--invoke", action="store_true", help="PAID: actually invoke the runtimes")
    ap.add_argument("--log-wait", type=int, default=60, help="seconds to wait for log lines per runtime")
    ap.add_argument("--prompt-file", default=os.path.join(HERE, "sample_prompt.txt"),
                    help="prompt data to send (default: the sample; afc_tools/real_prompt.txt = a real match state)")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    print(f"region={session.region_name} profile={args.profile or '(default chain)'}")
    print("identity:", session.client("sts").get_caller_identity()["Arn"])
    arns = runtime_arns(session.client("cloudformation"))
    with open(args.prompt_file, encoding="utf-8") as f:
        base = json.load(f)
    team_id = base.get("teamId", 0)
    agents = [a for a in AGENTS if not args.only or a[0] in args.only]

    if not args.invoke:
        for key, _, pid in agents:
            print(f"  plan {key:5s} player {pid} team {team_id} -> {arns[key]}")
        print("Plan only. Re-run with --invoke to call the runtimes (paid).")
        return 0

    client = session.client("bedrock-agentcore", config=Config(read_timeout=180, retries={"max_attempts": 1}))
    logs = session.client("logs")
    report = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "region": session.region_name, "stack": STACK, "results": []}
    failures = 0
    for key, _, pid in agents:
        prompt_data = {**base, "myPlayers": [pid]}
        prompt = json.dumps(prompt_data, separators=(",", ":"))
        payload = json.dumps({"prompt": prompt}).encode()
        row = {"agent": key, "player": pid, "team": team_id, "runtimeArn": arns[key],
               "payloadSha256": hashlib.sha256(payload).hexdigest()[:16]}
        print(f"\n== {key.upper()} (player {pid}) {arns[key]}")
        if not arns[key]:
            row["error"] = "runtime ARN not in stack outputs"
            failures += 1
            report["results"].append(row)
            print("  ", row["error"])
            continue
        start_ms = int(time.time() * 1000)
        t0 = time.perf_counter()
        try:
            r = client.invoke_agent_runtime(agentRuntimeArn=arns[key], qualifier="DEFAULT",
                                            runtimeSessionId=f"afc-e2e-{uuid.uuid4().hex}",
                                            contentType="application/json", accept="application/json, text/event-stream",
                                            payload=payload)
            body = r["response"].read()
            row.update(latencyS=round(time.perf_counter() - t0, 2), statusCode=r.get("statusCode"),
                       contentType=r.get("contentType"))
            reply = decode_reply(body, r.get("contentType"))
            row["reply"] = reply[:2000]
        except (ClientError, BotoCoreError) as e:
            row.update(latencyS=round(time.perf_counter() - t0, 2), error=err(e))
            failures += 1
            report["results"].append(row)
            print("   invoke FAILED:", row["error"])
            continue
        # AFC: a server-frame state is checked like the runtime sees it: the reply scaled back to the design
        # pitch against the converted state, plus "every MOVE_TO is on the server pitch".
        design_state, scale = to_design_frame(prompt_data["gameState"])
        raw_reply = extract_reply(reply)
        if scale != 1.0 and isinstance(raw_reply, list):
            for c in raw_reply:
                p = c.get("parameters") if isinstance(c, dict) else None
                if c.get("commandType") == "MOVE_TO" and isinstance(p, dict):
                    on_pitch = abs(p.get("target_x", 0)) <= SERVER_HALF_LENGTH and abs(p.get("target_y", 0)) <= SERVER_HALF_WIDTH + 0.01
                    print(f"   server pitch: MOVE_TO ({p.get('target_x')}, {p.get('target_y')}) on the pitch: {on_pitch}")
                    failures += 0 if on_pitch else 1
                    p.update(target_x=p.get("target_x", 0) * scale, target_y=p.get("target_y", 0) * scale)
        ctx = context_from_state(design_state, team_id, pid)
        verdict = validate_reply(raw_reply, ctx)
        row.update(valid=verdict.ok, reason=verdict.reason, issues=[str(i) for i in verdict.issues],
                   commands=list(verdict.commands))
        source, evidence = attribute(logs, arns[key].split("/")[-1], start_ms, args.log_wait)
        row.update(source=source, logEvidence=[e[:300] for e in evidence])
        failures += not verdict.ok
        report["results"].append(row)
        print(f"   status={row['statusCode']} latency={row['latencyS']}s contentType={row['contentType']}")
        print(f"   reply: {reply[:300]}")
        print(f"   valid for player {pid}/team {team_id}: {verdict.ok} ({verdict.reason}) {row['issues'] or ''}")
        print(f"   source (from logs): {source}")
        for line in row["logEvidence"]:
            print(f"     log: {line[:200]}")

    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    out = os.path.join(HERE, "reports", f"e2e-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\n{len(agents) - failures}/{len(agents)} runtimes returned a valid command | report: {os.path.relpath(out, ROOT)}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
