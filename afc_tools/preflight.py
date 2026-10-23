"""READ-ONLY AWS preflight for the tournament copy. Never invokes a model, never creates,
changes or deletes anything, never prints credentials (only where they came from).

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\preflight.py --region us-east-1

Checks, in order:
  1. identity        sts:GetCallerIdentity (account, ARN) + which credential source was used
  2. availability    for every model_id in the balanced team's main.py files:
                     bedrock:GetInferenceProfile (status, destination regions), then per
                     destination region bedrock:GetFoundationModel (lifecycle) and
                     bedrock:GetFoundationModelAvailability (agreement/authorization/entitlement)
  3. permission      iam:SimulatePrincipalPolicy for bedrock:InvokeModel(+WithResponseStream)
                     on the profile and its foundation models. Simulation ignores some
                     controls (e.g. SCPs in some setups): only a real call proves access.
  4. deploy state    CloudFormation stacks CDKToolkit (bootstrap) and the AgentCore stack,
                     and existing AgentCore runtimes (name collisions)
Any check this identity may not call is reported as "not determinable", not as a failure.
"""

import argparse
import glob
import os
import re
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

HERE = os.path.dirname(os.path.abspath(__file__))
TEAM = os.path.join(os.path.dirname(HERE), "ai-team-strands-balanced")
STACK = "AgentCore-TeamStrandsBalanced-default"
ACTIONS = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]


def model_ids():
    ids = {}
    for path in sorted(glob.glob(os.path.join(TEAM, "ai-*", "src", "main.py"))):
        with open(path, encoding="utf-8") as f:
            for m in re.findall(r'model_id="([^"]+)"', f.read()):
                ids.setdefault(m, []).append(os.path.basename(os.path.dirname(os.path.dirname(path))))
    return ids


def err(e):
    if isinstance(e, ClientError):
        return f"{e.response['Error'].get('Code')}: {e.response['Error'].get('Message', '')[:120]}"
    return f"{type(e).__name__}: {str(e)[:120]}"


def principal_arn(arn):
    m = re.match(r"arn:(aws[\w-]*):sts::(\d+):assumed-role/([^/]+)/", arn)
    if m:  # role path is not in the session ARN; a role with a path will fail simulation
        return f"arn:{m.group(1)}:iam::{m.group(2)}:role/{m.group(3)}"
    return None if arn.endswith(":root") else arn


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    a = ap.parse_args()

    session = boto3.Session(profile_name=a.profile) if a.profile else boto3.Session()
    region = a.region or session.region_name
    blockers = []
    print("== 1. identity")
    if not region:
        print("  BLOCKED: no region. Pass --region or set AWS_REGION (deploy_all.py would default to us-east-1,")
        print("           a local Strands BedrockModel to us-west-2: pick one explicitly).")
        return 2
    try:
        creds = session.get_credentials()
    except BotoCoreError as e:  # e.g. `aws login` profiles need botocore[crt] in this venv
        print(f"  BLOCKED: credentials for this profile could not be loaded ({err(e)})")
        return 2
    if creds is None:
        print("  BLOCKED: no AWS credentials found (no env vars, profile, SSO cache or role).")
        return 2
    print(f"  credential source: {creds.method}   region: {region}   profile: {a.profile or '(default chain)'}")
    try:
        ident = session.client("sts", region_name=region).get_caller_identity()
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        print(f"  BLOCKED: sts:GetCallerIdentity failed ({err(e)})")
        return 2
    account, arn = ident["Account"], ident["Arn"]
    print(f"  account: {account}   arn: {arn}")
    if arn.endswith(":root"):
        print("  WARN: these are ROOT credentials; use an IAM role/user for the event")

    bedrock = session.client("bedrock", region_name=region)
    iam = session.client("iam", region_name=region)
    pa = principal_arn(arn)
    for pid, agents in model_ids().items():
        print(f"== 2/3. model {pid}  (used by {', '.join(agents)})")
        resources = []
        try:
            prof = bedrock.get_inference_profile(inferenceProfileIdentifier=pid)
            arns = [m["modelArn"] for m in prof.get("models", [])]
            regions = sorted({x.split(":")[3] for x in arns})
            print(f"  inference profile: {prof.get('status')}  type {prof.get('type')}  destinations {regions}")
            resources = [prof["inferenceProfileArn"], *arns]
        except (ClientError, BotoCoreError) as e:
            print(f"  inference profile: not determinable ({err(e)})")
            arns = []
        base = pid.split(".", 1)[1] if pid.startswith(("us.", "eu.", "apac.", "global.")) else pid
        for r in sorted({x.split(":")[3] for x in arns} or {region}):
            b = bedrock if r == region else session.client("bedrock", region_name=r)
            try:
                fm = b.get_foundation_model(modelIdentifier=base)["modelDetails"]
                life = fm.get("modelLifecycle", {}).get("status")
            except (ClientError, BotoCoreError) as e:
                life = f"not determinable ({err(e)})"
            try:
                av = b.get_foundation_model_availability(modelId=base)
                avail = {k: (v.get("status") if isinstance(v, dict) else v) for k, v in av.items()
                         if k in ("agreementAvailability", "authorizationStatus", "entitlementAvailability", "regionAvailability")}
            except (ClientError, BotoCoreError) as e:
                avail = f"not determinable ({err(e)})"
            print(f"  {r}: {base} lifecycle={life} availability={avail}")
        if pa and resources:
            try:
                res = iam.simulate_principal_policy(PolicySourceArn=pa, ActionNames=ACTIONS, ResourceArns=resources)
                decisions = sorted({(x["EvalActionName"], x["EvalDecision"]) for x in res["EvaluationResults"]})
                denied = [d for d in decisions if d[1] != "allowed"]
                print(f"  IAM simulation for {pa}: " + ("all allowed" if not denied else f"NOT ALLOWED {denied}"))
                if denied:
                    blockers.append(f"{pid}: IAM does not allow {sorted({d[0] for d in denied})}")
            except (ClientError, BotoCoreError) as e:
                print(f"  IAM simulation: not determinable ({err(e)})")
        elif not pa:
            print("  IAM simulation: skipped for root credentials")

    print("== 4. deployment state (read-only)")
    cfn = session.client("cloudformation", region_name=region)
    for name in ("CDKToolkit", STACK):
        try:
            st = cfn.describe_stacks(StackName=name)["Stacks"][0]
            outs = {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", []) if o["OutputKey"] == "BootstrapVersion"}
            print(f"  stack {name}: {st['StackStatus']} {outs or ''}")
        except ClientError as e:
            missing = "does not exist" in str(e)
            print(f"  stack {name}: " + ("NOT PRESENT" if missing else f"not determinable ({err(e)})")
                  + ("  -> `cdk bootstrap` would CREATE it (needs approval)" if missing and name == "CDKToolkit" else ""))
    try:
        rts = session.client("bedrock-agentcore-control", region_name=region).list_agent_runtimes().get("agentRuntimes", [])
        names = sorted(r.get("agentRuntimeName", "?") for r in rts)
        print(f"  existing AgentCore runtimes in {region}: {len(names)} {names[:10]}")
    except (ClientError, BotoCoreError) as e:
        print(f"  AgentCore runtimes: not determinable ({err(e)})")

    print("== result: " + ("no blockers found by read-only checks (a real call is still the only proof)"
                           if not blockers else "BLOCKERS: " + "; ".join(blockers)))
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
