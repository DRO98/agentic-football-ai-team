"""PAID (tiny): which candidate models can THIS account call, with which no-thinking setting, and how fast.

    python afc_tools\\model_probe.py            # plan only
    python afc_tools\\model_probe.py --invoke   # 2 short Converse calls per candidate (~20 calls, well under $0.01)

Each candidate gets the same one-line soccer prompt (~70 input tokens, <= 300 output). Reported per candidate:
accepted or the exact error (AccessDenied = no model access for this role/account; ValidationException = the
request field is not accepted), wall latency of each call, Bedrock latencyMs, tokens, and the start of the reply.
Claude models think before answering by default, which costs latency; the variants below try to turn that off.
No retries. Nothing deployed is touched.
"""

import argparse
import os
import time

import boto3
from botocore.config import Config

SYSTEM = [{"text": "You control one soccer player. Reply ONLY with a JSON array holding exactly one command."}]
PROMPT = [{"role": "user", "content": [{"text": (
    'Ball at (10,2) held by OPP player 3. You are player 1 at (0,0). Answer like '
    '[{"commandType":"PRESS_BALL","playerId":1,"parameters":{"intensity":0.8},"duration":3}]')}]}]

# (name, inference profile id, additionalModelRequestFields or None)
CANDIDATES = [
    ("nova-micro", "us.amazon.nova-micro-v1:0", None),
    ("nova-lite", "us.amazon.nova-lite-v1:0", None),
    ("nova-pro", "us.amazon.nova-pro-v1:0", None),
    ("nova-2-lite", "us.amazon.nova-2-lite-v1:0", None),
    ("haiku-4-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0", None),
    ("haiku-5-5 (default)", "us.anthropic.claude-haiku-5-5", None),
    ("haiku-5-5 thinking off", "us.anthropic.claude-haiku-5-5", {"thinking": {"type": "disabled"}}),
    ("haiku-5-5 effort low", "us.anthropic.claude-haiku-5-5", {"output_config": {"effort": "low"}}),
    ("sonnet-5-5 thinking off", "us.anthropic.claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
    ("sonnet-5-5 effort low", "us.anthropic.claude-sonnet-5-5", {"output_config": {"effort": "low"}}),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--invoke", action="store_true", help="PAID (tiny): make the calls")
    args = ap.parse_args()
    s = boto3.Session(profile_name=args.profile, region_name=args.region)
    print("identity:", s.client("sts").get_caller_identity()["Arn"])
    if not args.invoke:
        for name, mid, extra in CANDIDATES:
            print(f"  plan {name:26s} {mid} {extra or ''}")
        print("Plan only. Re-run with --invoke (2 calls per candidate).")
        return 0
    rt = s.client("bedrock-runtime", config=Config(retries={"total_max_attempts": 1}, read_timeout=60))
    for name, mid, extra in CANDIDATES:
        lat, info, err = [], None, None
        for _ in range(2):
            kw = dict(modelId=mid, system=SYSTEM, messages=PROMPT, inferenceConfig={"maxTokens": 300})
            if extra:
                kw["additionalModelRequestFields"] = extra
            t0 = time.perf_counter()
            try:
                r = rt.converse(**kw)
            except Exception as e:  # recorded, never retried
                err = f"{type(e).__name__}: {str(e)[:150]}"
                break
            lat.append(round(time.perf_counter() - t0, 2))
            text = "".join(b.get("text", "") for b in r["output"]["message"]["content"]).strip().replace("\n", " ")
            info = (f"bedrock {r['metrics']['latencyMs']} ms | tokens {r['usage']['inputTokens']}/{r['usage']['outputTokens']}"
                    f" | {text[:70]}")
        print(f"{name:26s} " + (f"OK   wall {lat} s | {info}" if not err else f"FAIL {err}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
