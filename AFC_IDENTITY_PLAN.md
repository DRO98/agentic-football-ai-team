# Moving off the root user: plan (not executed)

**Current state:** the `afc` profile (`aws login`) signs in as `arn:aws:iam::<PERSONAL_ACCOUNT_ID>:root`. Root can't be restricted by IAM policies, and it can change billing, support and contact settings or close the account. It should be used only to set up the identity below, with MFA, and then put away.

**Target:** an IAM user `afc-dev` that signs in to the console with a password **plus MFA**. The CLI gets short-term credentials through `aws login`, so **no access keys exist anywhere**.

IAM Identity Center is the other AWS-recommended option. For access to AWS accounts it needs an AWS Organization, which is a bigger account change than this hackathon needs.

## Permissions, by phase
| Phase | What runs | Permissions |
|---|---|---|
| 1. Read-only checks | `afc_tools\preflight.py` | Policy A below |
| 2. Model testing (paid, but no resources) | `test_local.py --llm`; local Strands calls | Policy A + Policy B |
| 3. Bootstrap + deploy (creates resources) | `cdk bootstrap`, `agentcore deploy/invoke/logs/status` | Admin-level, time-boxed; see below |

### Policy A: read-only preflight (inline policy `AfcReadOnlyChecks`)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "BedrockModelMetadata", "Effect": "Allow",
     "Action": ["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles", "bedrock:GetFoundationModel",
                "bedrock:ListFoundationModels", "bedrock:GetFoundationModelAvailability"],
     "Resource": "*"},
    {"Sid": "DeploymentStateRead", "Effect": "Allow",
     "Action": ["cloudformation:DescribeStacks", "bedrock-agentcore:ListAgentRuntimes"],
     "Resource": "*"},
    {"Sid": "SimulateOwnPolicies", "Effect": "Allow",
     "Action": "iam:SimulatePrincipalPolicy",
     "Resource": "arn:aws:iam::<PERSONAL_ACCOUNT_ID>:user/afc-dev"}
  ]
}
```
`sts:GetCallerIdentity` needs no permission.

### Policy B: Nova model calls through the US inference profiles (inline policy `AfcNovaInvoke`)
A cross-region profile needs permission on the profile and on the foundation model in every destination region. Preflight showed the `us.` profiles route to us-east-1, us-east-2 and us-west-2.
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "InvokeNovaViaUsProfiles", "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": [
       "arn:aws:bedrock:us-east-1:<PERSONAL_ACCOUNT_ID>:inference-profile/us.amazon.nova-micro-v1:0",
       "arn:aws:bedrock:us-east-1:<PERSONAL_ACCOUNT_ID>:inference-profile/us.amazon.nova-lite-v1:0",
       "arn:aws:bedrock:us-east-1:<PERSONAL_ACCOUNT_ID>:inference-profile/us.amazon.nova-pro-v1:0",
       "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-*",
       "arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-*",
       "arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-*"
     ]}
  ]
}
```

### Phase 3: deployment
- **`cdk bootstrap`** creates IAM roles, an S3 bucket, an ECR repository and an SSM parameter, so it needs admin-level rights once.
- **Deploying** goes through the bootstrap roles (`cdk-hnb659fds-*-role-<PERSONAL_ACCOUNT_ID>-us-east-1`, via `sts:AssumeRole`). The AgentCore CLI also makes direct calls for status, invoke and logs, and I have **not verified** that list: it can only be seen by running it.
- **Pragmatic choice:** attach the AWS-managed `AdministratorAccess` to `afc-dev` **only for the bootstrap + deploy window**, with MFA and no keys. Remove it afterwards and keep A + B. This is still much safer than root.
- **Tighten afterwards:** IAM Access Analyzer can generate a least-privilege policy from the CloudTrail record of what was actually used. Use that for the next deploy.

## Steps you perform (console; this repo does none of them)
1. Sign in as **root** and make sure **MFA is enabled for root** (Account → Security credentials).
2. IAM → Users → **Create user** `afc-dev`:
   - tick "Provide user access to the AWS Management Console", IAM user, custom password;
   - **do not create access keys**.
3. Add inline policies **A** and **B** (JSON above).
4. Sign out of root. Sign in as `afc-dev` with the account sign-in URL (`https://<PERSONAL_ACCOUNT_ID>.signin.aws.amazon.com/console`) and **assign an MFA device** to `afc-dev`.
5. Locally, in a new terminal:
   ```powershell
   aws login --profile afc-dev --region us-east-1      # sign in as the IAM user afc-dev, NOT root
   aws sts get-caller-identity --profile afc-dev        # expect arn:aws:iam::<PERSONAL_ACCOUNT_ID>:user/afc-dev
   ```
   If the `aws login` browser flow doesn't let you pick an IAM user, stop and say so. Don't fall back to access keys.
6. I re-run the read-only preflight with `--profile afc-dev`. The IAM permission simulation then runs, which is the check for Policy B.
7. Stop using `afc`. Clear its cached root session with `aws logout --profile afc`, if `aws logout help` shows that command in your CLI.
8. Before deploying: temporarily attach `AdministratorAccess` to `afc-dev`, then remove it after the deploy (step 3 of the runbook's deploy section).
9. Optional: a billing budget alert (Billing → Budgets).
