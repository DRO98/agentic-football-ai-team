"""Remove the PERSONAL-account deployment (<PERSONAL_ACCOUNT_ID>) after the event. Never touches any other account.

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\cleanup_personal.py            # plan: read-only
    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\cleanup_personal.py --delete   # deletes the stack

Like `cdk destroy`: afc-dev assumes the CDK deploy role and asks CloudFormation to delete the stack with
the bootstrap's execution role. That removes the 5 AgentCore runtimes and their IAM roles/policies.
Kept on purpose: CDKToolkit (bootstrap; delete in the console if no more deploys), the runtime log groups
/aws/bedrock-agentcore/runtimes/* (afc-dev may not delete them; console, optional), and the afc-dev user
(see AFC_MATCHDAY_CHECKLIST.md section 7). The event account is left alone by construction: the profile,
account id and stack name are fixed below and checked before anything is called.
"""

import argparse
import sys
import time

import boto3

PROFILE = "afc-dev"
ACCOUNT = "<PERSONAL_ACCOUNT_ID>"
REGION = "us-east-1"
STACK = "AgentCore-TeamStrandsBalanced-default"
DEPLOY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-deploy-role-{ACCOUNT}-{REGION}"
EXEC_ROLE = f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-cfn-exec-role-{ACCOUNT}-{REGION}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--delete", action="store_true", help="DELETE the stack (otherwise plan only)")
    args = ap.parse_args()

    base = boto3.Session(profile_name=PROFILE, region_name=REGION)
    me = base.client("sts").get_caller_identity()
    print(f"identity: {me['Arn']}")
    if me["Account"] != ACCOUNT:
        print(f"REFUSED: profile {PROFILE} is account {me['Account']}, expected {ACCOUNT}")
        return 3

    creds = base.client("sts").assume_role(RoleArn=DEPLOY_ROLE, RoleSessionName="afc-cleanup")["Credentials"]
    cfn = boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"], region_name=REGION).client("cloudformation")
    try:
        st = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    except cfn.exceptions.ClientError as e:
        if "does not exist" in str(e):
            print(f"stack {STACK}: not present in {ACCOUNT} — nothing to delete")
            return 0
        raise
    if f":{ACCOUNT}:" not in st["StackId"]:
        print(f"REFUSED: stack id {st['StackId']} is not in {ACCOUNT}")
        return 3
    res = cfn.describe_stack_resources(StackName=STACK)["StackResources"]
    print(f"stack {STACK}: {st['StackStatus']} (last update {st.get('LastUpdatedTime')})")
    for r in res:
        print(f"   {r['ResourceType']:34s} {r['PhysicalResourceId']}")
    if not args.delete:
        print("Plan only. Re-run with --delete to remove these resources.")
        return 0

    print(f"deleting {STACK} in {ACCOUNT} ...")
    cfn.delete_stack(StackName=STACK, RoleARN=EXEC_ROLE)
    while True:
        time.sleep(15)
        try:
            status = cfn.describe_stacks(StackName=st["StackId"])["Stacks"][0]["StackStatus"]
        except cfn.exceptions.ClientError as e:
            print(f"   {e}")
            return 1
        print(f"   {status}")
        if status == "DELETE_COMPLETE":
            print("done: runtimes, roles and policies removed")
            return 0
        if status.endswith("FAILED"):
            for ev in cfn.describe_stack_events(StackName=st["StackId"])["StackEvents"][:10]:
                if ev["ResourceStatus"].endswith("FAILED"):
                    print(f"   {ev['LogicalResourceId']}: {ev.get('ResourceStatusReason')}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
