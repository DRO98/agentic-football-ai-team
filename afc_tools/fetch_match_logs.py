"""READ-ONLY: download the useful log lines of our five runtimes for the last N minutes into one file,
so a match can be analysed offline (no AWS access needed afterwards).

    python afc_tools\\fetch_match_logs.py --minutes 30 --out C:\\dev\\afc\\captures\\match1.log

Kept lines (message text only, one per line, prefixed with the agent): LAB_CAPTURE (full game state, GK
only while AFC_CAPTURE_STATE=1), validator rejections, rule answers, "LLM returned", AFC_PROTOCOL warnings
and agent errors. The OpenTelemetry JSON copies of the same lines are skipped. Runtime ARNs come from the
CloudFormation stack in whatever account the current credentials belong to.
"""

import argparse
import json
import os
import sys
import time

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from e2e_invoke import STACK, runtime_arns  # noqa: E402

KEEP = ("LAB_CAPTURE ", "rejected by validator", "answered by rule", "LLM returned", "AFC_PROTOCOL",
        "agent error", "using fallback", "last resort", "agent invoked for team")


def message(raw):
    """The plain log message: the runtime writes JSON lines with a "message" field; skip OTEL copies."""
    s = raw.strip()
    if s.startswith("{"):
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            return None
        if "resource" in ev:  # OpenTelemetry copy
            return None
        return ev.get("message")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--out", required=True)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    args = ap.parse_args()

    s = boto3.Session(profile_name=args.profile, region_name=args.region)
    print("identity:", s.client("sts").get_caller_identity()["Arn"])
    arns = runtime_arns(s.client("cloudformation"))
    logs = s.client("logs")
    start = int((time.time() - args.minutes * 60) * 1000)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    counts = {}
    with open(args.out, "w", encoding="utf-8") as f:
        for key, arn in arns.items():
            if not arn:
                print(f"  {key}: no runtime in {STACK}")
                continue
            group = f"/aws/bedrock-agentcore/runtimes/{arn.split('/')[-1]}-DEFAULT"
            kw, n = {"logGroupName": group, "startTime": start}, 0
            while True:
                r = logs.filter_log_events(**kw)
                for e in r["events"]:
                    m = message(e["message"])
                    if m and any(k in m for k in KEEP):
                        f.write(f"{e['timestamp']}\t{key}\t{m}\n")
                        n += 1
                if not r.get("nextToken"):
                    break
                kw["nextToken"] = r["nextToken"]
            counts[key] = n
            print(f"  {key}: {n} lines")
    print(f"wrote {args.out}  ({os.path.getsize(args.out) // 1024} KB)")


if __name__ == "__main__":
    main()
