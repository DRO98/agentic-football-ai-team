# AFC tournament copy — runbook

Working copy of `aws-samples/sample-ai-possibilities` (commit 15279eb), only the `agentic-football-sample-agents` folder, published as its own repository. Account ids are redacted as `<EVENT_ACCOUNT_ID>` and `<PERSONAL_ACCOUNT_ID>`.

## What differs from upstream
The lab code below was ported **verbatim**; only imports and docstrings changed. A parity check against the lab gave 0 differences across 360 summaries, 360 fallbacks and 5,040 validator verdicts.

| File | Change |
|---|---|
| `lib/possession.py` (new) | Team-aware possession OURS/THEIRS/LOOSE/UNKNOWN (lab Exp 2a) |
| `lib/stamina.py` (new) | Precise stamina, `unknown` when missing or invalid (lab Exp 5) |
| `lib/action_schema.py`, `lib/action_validator.py` (new) | The command contract and the validator (lab Exp 3) |
| `lib/state.py` | Lab Exp 5 summary (possession + stamina), plus safe `gameTime`: a finite number renders as before, anything else `Time: unknown` (lab Exp 8) |
| `lib/fallback.py` | Lab Exp 2a possession-aware rules: on-ball actions only when OURS and me, press only when THEIRS, GK intercepts a LOOSE ball, UNKNOWN holds position |
| `lib/agent_base.py` | Layer 1 validates the raw reply; the fallback is validated too, then last resort with the payload's teamId. **History:** `SlidingWindowConversationManager(window_size=AFC_HISTORY_WINDOW)`, default **0**. Optional `AFC_CAPTURE_STATE` capture line. `create_agent(model=...)` for offline tests. |
| `ai-gk/src/main.py` | GK prompt: the two anchored lab lines ("held by free" → INTERCEPT; UNKNOWN or `hasBall=unknown` → never GK_DISTRIBUTE) |
| `ai-*/test_local.py` | **Fixtures only, assertions unchanged.** "Has the ball" states now put the ball at its holder. FWD1's press test used `agentId_6`, which no player has; it now uses away #1 on the ball. Lines are marked `# AFC:`. |
| `lib/rules.py` (new) | Deterministic pre-model rule for the GK: a LOOSE ball inside our penalty area → INTERCEPT, validated, model not called. Box = 16.5 deep × ±20.16 wide (**assumption**: standard pitch in metres). **DEF (2026-10-22):** an outfield teammate on our ball → MOVE_TO (y 0) 15 behind our most advanced outfield teammate, never past halfway |
| `ai-def/src/main.py` (2026-10-22) | DEF prompt: slide only as a last resort near our goal (distToMyGoal under 35), never in their half / on a sideline / in their corner; our-ball shape line; `rule_fn=def_shape_on_our_ball` |
| `lib/action_schema.py`, `lib/action_validator.py` (AFC lines) | PRESS_BALL is rejected when the ball is OURS (`own_ball`); THEIRS, LOOSE and UNKNOWN still allowed |
| `ai-*/src/main.py` | One prompt line in all five: `"duration" must be a whole number >= 0, never negative.`; GK passes `rule_fn=gk_loose_ball_in_box` |
| `afc_tests/` | 63 offline tests (16 of them for `model_eval.py`: plan, scoring, heuristics, budget guards, no hidden retries), plus 47: 7 gameTime/capture, 20 integration (stamina, possession, validator, fallback, history), 20 tactics (press/intercept on own ball, duration and playerId prompt lines, GK rule, the 3 real FWD1 FOLLOW_PLAYER rejections, protocol warning, a short deterministic simulated match) |

### Assumptions that real captures must confirm (behaviour stays conservative)
- **Possession by proximity** is used only when both teams carry the same id (as in the sample). It needs `CONTROL_RADIUS` 3.0 and `MIN_SEPARATION` 5.0 pitch units. Both are **unverified guesses**; anything else becomes UNKNOWN, and the agent holds position.
- `TEAM_FIELD_CANDIDATES` (`possessionTeamCode`/`possessionTeamId`/`possessionTeam`) are **placeholders**; no such field exists in the sample.
- **Stamina range** 0–1 is read from the sample; values outside it show `unknown`.
- **Validator contract** comes from the upstream prompts, README and parser, not from the server. Pitch ±55/±35, exactly one command per reply, target ids 0–4.
- **Not ported (semantics unverified):** Exp 6 sprint/playMode and ball velocity.

### History setting
- **Default:** `AFC_HISTORY_WINDOW` unset → window 0. Strands removes every non-pinned message after each call, so each tick sends the system prompt + the current summary, and nothing carries over between matches.
- **Override:** an integer ≥ 0 sets a bounded window, read when the agent is created. On AgentCore that's a runtime `envVars` entry and a redeploy. A missing, non-integer or negative value means 0. `NullConversationManager` is not used, because it never trims.
- **Not measured:** the effect on play quality.
| `afc_tools/preflight.py` | **Read-only** AWS checks: identity, model availability vs IAM permission, bootstrap state, existing runtimes |
| `afc_tools/with_lib.py` | Runs one command with `lib/` staged into each runtime (like `deploy_all.py`), without bootstrapping and without adding `--yes` |
| `afc_tools/sample_prompt.txt` | The upstream SAMPLE state as an invoke prompt (test data, not a real capture) |
| `afc_tools/e2e_invoke.py` | Plan by default; `--invoke` (paid) calls each deployed runtime once, validates the reply for its player/team, attributes it (model / validator fallback / error) from the runtime logs, writes `afc_tools/reports/e2e-*.json` |
| `afc_tools/team_tick.py` | Plan by default; `--invoke` (paid, 5 per scenario) sends 5 scenario ticks to all five runtimes concurrently, validates every reply, warns on team-level conflicts, attributes each reply by session id, writes `afc_tools/reports/team-*.json` |
| `afc_tools/sim_match.py` | Offline SIMULATED matches (toy physics, scripted opponent) through the real handler pipeline: validity, possession compatibility, inference vs truth, rule situations |
| `afc_tools/model_eval.py` | Controlled model × prompt-variant comparison for one position through our summary + validator. Plan by default; `--invoke` is paid |
| `AFC_MATCHDAY_CHECKLIST.md` | Match-day checklist (Spanish) |
| `afc_tools/match_watch.py` | **Read-only** match monitor from the runtimes' logs: calls vs sessions (session reuse = warm), team/player the server asked for, tick interval, handler time, reply sources, captures |
| `afc_tools/constraints-deployed-20261018.txt` | Exact versions (113 distributions) of the deployed code zips. Pass as `UV_CONSTRAINT` when deploying so a redeploy ships the same dependencies |
| `afc_iam/*.json` | Least-privilege customer managed policies: `AfcCdkBootstrapResources` + `AfcCdkDeploy` (attached to `afc-dev`), `AfcCfnExecAgentCore` (the bootstrap's CloudFormation execution policy instead of `AdministratorAccess`) |

Everything else, including prompts, models, fallbacks and `agentcore.json`, is upstream.

## Tools (local to `C:\dev\afc`, nothing installed globally)
- `.venv\` is Python 3.12 with `strands-agents==1.61.1`, `bedrock-agentcore` 1.24.1, `uv` 0.13.1, and `botocore[crt]` (`awscrt` 0.36.0). The CRT extra is needed to read `aws login` credentials from Python.
- **Identity:** the `afc` profile is the **root** user. See `AFC_IDENTITY_PLAN.md` before deploying.
- `tools\node_modules\.bin\` holds `agentcore` 0.31.1 (telemetry disabled) and `cdk` 2.1126.0.
- **Not installed, manual step:** AWS CLI v2. It is optional for these steps, but needed for `aws sso login` / `aws configure`, and `deploy_all.py` requires it.

Open each PowerShell session with:
```powershell
$env:PATH = "C:\dev\afc\tools\node_modules\.bin;C:\dev\afc\.venv\Scripts;" + $env:PATH
$env:CDK_DISABLE_CLI_TELEMETRY = "true"
$env:AWS_REGION = "us-east-1"; $env:AWS_DEFAULT_REGION = $env:AWS_REGION   # pick ONE region and keep it
# credentials: $env:AWS_PROFILE = "<profile>"  or the event's temporary env vars (never paste them into files)
cd C:\dev\afc\agentic-football-sample-agents
```

## 1. Read-only checks (free; no approval needed)
```powershell
python afc_tools\preflight.py
```

## 2. First real model call (paid; needs approval)
- **What it is:** one Nova Micro call from this laptop. There is no deployment. The input is about 690 tokens by the chars/4 estimate (GK prompt 2,167 chars plus summary 595), and the output is at most a few dozen tokens.
- **Cost:** a fraction of a cent at Nova Micro's on-demand price. Check the current Bedrock price for your region.
- **Command:**
```powershell
cd ai-team-strands-balanced\ai-gk; python test_local.py --llm; cd ..\..
```
- **Pass condition:** the output ends with `LLM test PASSED — all commands for player 0`. An error naming AccessDenied, model access or region is the blocker to fix.
- **Region:** without `AWS_REGION`, a local `BedrockModel` uses **us-west-2** (strands default).

## 3. Review the deployment (read-only; needs credentials, no approval)
```powershell
cd ai-team-strands-balanced
python ..\afc_tools\with_lib.py --write-target agentcore validate     # writes agentcore/aws-targets.json (local file)
python ..\afc_tools\with_lib.py agentcore deploy --dry-run
python ..\afc_tools\with_lib.py agentcore deploy --diff
```
If `preflight` reported `CDKToolkit: NOT PRESENT`, the account/region needs a one-time bootstrap first (see 4a).

## 4. Deploy (creates resources and ongoing charges; needs approval)
- **Done on 2026-10-18 (us-east-1, as `afc-dev`):** bootstrap v32 with the scoped execution policy, then the deploy below. E2E: 5/5 runtimes returned a valid command (`afc_tools/reports/e2e-20261018-183830.json`).
- **Tactical fixes deployed (GK v3, others v2):** team run 25/25 valid (`afc_tools/reports/team-20261018-191908.json`); DEF/MID rejected with `own_ball` when our GK holds the ball; GK answered the loose ball in its box by rule. **Latency:** that rule call took 7.1 s with no model call, so the 6–9 s per invocation is mostly per-session start-up (each call used a new `runtimeSessionId`), not Bedrock.
- **Warm sessions measured** (`team_tick.py --invoke --reuse-sessions`, `afc_tools/reports/team-20261018-192136.json`): first call per session 6.6–8.6 s (cold); later calls 0.58–0.97 s with a model call, 0.19 s for the GK rule; slowest of five per tick 0.73–0.97 s. 25/25 valid. **What the match server does with `runtimeSessionId` decides the tick rate**: a new id per tick means ~7–9 s ticks.
- **4a. Only if not bootstrapped** (run from `C:\dev\afc`, which has no `cdk.json`):
  `cdk bootstrap aws://<account>/<region> --cloudformation-execution-policies arn:aws:iam::<account>:policy/AfcCfnExecAgentCore`
  This creates the `CDKToolkit` stack: an S3 asset bucket (kept on delete), an ECR repository, deploy/publish/lookup IAM roles and an SSM version parameter. Without the flag, `cfn-exec-role` gets `AdministratorAccess`; `agentcore deploy`'s own auto-bootstrap does the same.
- **4b. Deploy:**
  `$env:AGENTCORE_SKIP_INSTALL = "1"; $env:UV_CONSTRAINT = "C:\dev\afc\agentic-football-sample-agents\afc_tools\constraints-deployed-20261018.txt"; python ..\afc_tools\with_lib.py agentcore deploy --target default`
  - `UV_CONSTRAINT`: the packager runs `uv pip install -r pyproject.toml` with only `>=` bounds, so without it each build pulls whatever is newest (on 2026-10-18 `httpx2`, `starlette` and `certifi` changed between builds an hour apart) and every runtime gets new code. With it, the zips are byte-identical to the deployed ones and `--diff` shows only real changes.
  - `--target default` takes the non-interactive path; no `--yes`, so it never bootstraps on its own.
  - `AGENTCORE_SKIP_INSTALL=1` stops the CLI from rewriting `agentcore/cdk/package.json` (`@aws/agentcore-cdk` alpha.45 → alpha.54) and running `npm install` mid-deploy, so the reviewed template is the one deployed. `--dry-run`/`--diff` only check dependencies.
  - The upstream equivalent, `python deploy_all.py`, always bootstraps and passes `--yes`, and it needs the AWS CLI.
- **Traces:** every runtime logs `Failed to export span batch code: 400`. AgentCore delivers spans only once CloudWatch Transaction Search is on for the account (no `aws/spans` log group exists). Commands and logs are unaffected. Fix: CloudWatch → Settings → X-Ray traces → Transaction Search → Enable (account-level; span ingestion is billed as CloudWatch Logs).
- **Capture:** on since 2026-10-18 for `ai_gk_agent` only (runtime version 2, `envVars` in `agentcore.json`). To switch off, remove that `envVars` entry and redeploy with the same two environment variables.
- **Creates:** CloudFormation stack `AgentCore-TeamStrandsBalanced-default` with 5 AgentCore Runtimes (CodeZip, PYTHON_3_12, PUBLIC, HTTP), one IAM execution role each, and code zips of about 38 MB each in the CDK asset bucket. Each role allows:
  - `bedrock:InvokeModel` / `InvokeModelWithResponseStream` / `CountTokens` on `foundation-model/*` and this account's `inference-profile/*`;
  - X-Ray writes;
  - logs to `/aws/bedrock-agentcore/runtimes/*`.

## 5. Invoke the deployed GK once (paid; needs approval)
```powershell
agentcore invoke --runtime ai_gk_agent --prompt-file ..\afc_tools\sample_prompt.txt
agentcore logs --help          # then read the GK runtime logs
```
The CLI sends the file's text as `prompt`. Verify on the first call that the agent receives `{"prompt": "<that JSON>"}`.

## First real match 2026-10-22 ~11:31 UTC (won 1-0 vs The Benchmark FC) → pitch-frame fix
- **Server protocol (verified):** sessions reused (1 per runtime per match), tick every 2.00 s, handler 0.4–0.6 s, each runtime asked for its own player, team 0. The prompt also carries `unlockedCommands` (`[]` on most ticks, the full command list on a few) and `previousCommand`.
- **Captures** (`afc_tools/fetch_match_logs.py` → `C:\dev\afc\captures\match1.log`; 70 real states kept in `afc_tests/fixtures/real_states_20261022.json`):
  - **The real pitch is ~8.6× smaller:** players stay within x ±6.4 and lateral ±3.6. The ball's lateral is `position.z`; its `y` is the height.
  - **The server executes MOVE_TO in its own units:** GK sent x -55 and stood at -5.93. Model targets like (25, 15) were clamped to corners → players sprinting to the edges; forwards at median stamina 0.16 / 0.11 (opponents 0.47).
  - **Possession was UNKNOWN on 45 of 70 ticks and OURS once** (ball read at (x, height)); the DEF rule fired 5 times in the match.
  - Ticks 0–4 of a match are in the sample's units (GKs at ±50); they are left alone.
- **Fix (`lib/frame.py`, wired in `lib/agent_base.py`):** a state whose players and ball all lie within |x| ≤ 8 is scaled ×55/6.4 into the design pitch (ball lateral from z); everything downstream is unchanged; outgoing MOVE_TO targets (clamped to ±6.4 × ±4.07) and FOLLOW/SLIDE distances are divided back. Design-frame states (sample, tests) pass through untouched.
  - Possession on the 70 real states: right 33, unknown 17, **wrong 0** (raw: right 1).
  - **Sprint limit** (`lib/rules.limit_sprint`): `sprint` forced off when my stamina (0–1) < 0.35.
  - Tests: `afc_tests/test_frame.py` (18, incl. all 5 agents × 70 real states: valid, on the pitch, no error/last resort). 139/139 offline, `sim_match.py` 0 invalid / 0 incompatible.
  - Local Bedrock check (afc-dev, 40 calls, real states): every MOVE_TO on the server pitch; GK holds x ≈ -5.9 tracking laterally; DEF rule 3/8.
  - `e2e_invoke.py --prompt-file afc_tools\real_prompt.txt` sends a real state (tick 4780) and checks MOVE_TO is on the server pitch.

## Matches 2 (1-2 vs Total Attack United, 11:40) and 3 (1-4, 11:47) → attack rules (not deployed with match 2/3; frame fix still pending then)
- Data: `C:\dev\afc\captures\match2.log` (covers matches 1–3; split in `matches_1_2_3.json`).
- **Our models almost never acted on the ball:** 3 SHOOT and ~8 PASS in three matches (possession UNKNOWN → on-ball commands blocked); the shots and passes seen on the pitch were mostly the server's own AI.
- **Shots fly at ~15 m/s** (ball velocity -14.5 / -15.2): under 1 s across the 12.8 m pitch, while a state arrives every 2 s. Our GK was centred on its line for 5 of 6 goals against; it cannot react to a shot. Goals against came from 9.5 and 11.4 m; two followed losing the ball in our own box (short GK distribution, DEF dribbling).
- One goal against followed the GK model's own MOVE_TO x 6.4 (the other goal).
- **New rules (`lib/rules.py`), all validated like model replies:**
  - `carrier_shoot` (DEF, MID, FWD1, FWD2): holding our ball within 55 design units of their goal (their half) → SHOOT, low corner away from their keeper (aim-letter orientation is an **assumption**), power 0.9. On the 166 real open-play states it fires 17 times (every time our carrier was in their half).
  - `fwd2_poacher` (FWD2): our ball held by a teammate → MOVE_TO (0.7 × their goal x, 12 lateral away from the ball); their ball further than 15 → wait at (0.2 × their goal x, 6 away from the ball) instead of pressing; otherwise the model.
  - `gk_leash` (output filter, GK): MOVE_TO never leaves our penalty area (16.5 deep, ±20.16).
  - `chain(...)`: DEF = carrier_shoot + def_shape; FWD2 = carrier_shoot + poacher.
- **Deployed to the event account 2026-10-22 ~12:04 UTC** together with the frame fix (prefixes gk `40bdcf8d…`, def `80ac7b41…`, mid `277070fd…`, fwd1 `6483d6f8…`, fwd2 `7cb94fcf…`; same ARNs). Checks: `e2e_invoke.py --invoke --prompt-file afc_tools\real_prompt.txt` 5/5, every MOVE_TO on the server pitch, DEF and FWD2 by rule (`afc_tools/reports/e2e-20261022-140629.json`); `team_tick.py --invoke --reuse-sessions` 25/25, FWD1 SHOOT by rule (`afc_tools/reports/team-20261022-140707.json`). The CLI reported "Transaction search enabled" in the event account. Rollback: `C:\dev\afc\rollback\deployed-20261022-def-shape\`.
- Tests: `afc_tests/test_attack_rules.py`; 157/157 offline, `sim_match.py` 0 invalid / 0 incompatible. Local Bedrock check (afc-dev, real states of matches 2–3): every command valid and on the server pitch; GK targets within its box.

## Match 4 (12:08, won 1-0; first match with the frame fix + attack rules) → shot angle and aim
- `C:\dev\afc\captures\match4.log` (also holds an 11:57 match, 3-0, still on the previous version).
- Every carrier of ours in their half was detected (5/5) and `carrier_shoot` answered (MID 1, FWD1 2, FWD2 3); FWD2 poacher 33 times.
- **Narrow goal:** in four matches every goal crossed the line within 0.8 m of the centre (~7 design units). The user saw many shots hit the post (we aimed BL/BR) and none from wide positions.
- Their keeper stood > 0.35 m off the centre on 73 % of open-play ticks (n 177).
- **`carrier_shoot` now:** from a tight angle (|lateral| > 7 + 0.5 × distance to their line, design units) → MOVE_TO up to 8 inward (lateral × 0.4) first; otherwise SHOOT at CENTER when their keeper is ≥ 3 off the centre, else BL/BR alternating by tick. Replayed on the four matches' states: SHOOT CENTER 21, BL/BR 16, cut inside 6.
- Rule answers now log their parameters (`... model not called: ['SHOOT'] [{"aim_location": ...}]`) so aims can be matched to outcomes.
- Tests 162/162, `sim_match.py` 0 invalid / 0 incompatible.

## Models compared + match 6 (12:24, lost 2-3) → MID on Nova 2 Lite, empty-goal shooting
- **Model access in the event account** (`afc_tools/model_probe.py --invoke`): Nova Micro/Lite/Pro, Nova 2 Lite, Claude Haiku 4.5 and Claude Sonnet 5.5 OK; **Claude Haiku 5.5 AccessDenied**. `afc-dev` (personal) may call Nova only.
- **`model_eval.py --scenario-set real`** (9 open-play states of match 1, converted like the runtime):
  - MID (`model-eval-mid-20261022-142721`, `-143026`): Nova Pro 100 % valid, p50/p95 0.81/1.20 s; **Nova 2 Lite 100 %, 0.64/0.93 s, same decisions**; Haiku 4.5 100 %, 1.03/1.57 s, not better; Sonnet 5.5 decided best (holds shape at stamina 0, supports instead of crowding) but p95 3.21 s (thinking off) / 2.99 s (effort low) > the 2 s tick.
  - FWD1 (`-143147`): Lite and Nova 2 Lite equal (100 %, ~0.65/0.9 s) → keep Lite. DEF (`-143217`): Lite's 2 rejections are a state the DEF rule answers → keep Lite.
  - **Change:** `ai-mid` → `us.amazon.nova-2-lite-v1:0`. Prices of Nova 2 Lite and Claude on Bedrock were not available to us (no `pricing:GetProducts`); `model_eval` uses placeholders for them.
- **Their #0 leaves its goal:** in six matches it stood more than 4 m from its goal line on about half of the open-play ticks, often on OUR goal line (Total Attack's GK scored). Coordinates are not mirrored (holder-to-ball 0.3/0.6 m raw vs 5–6 m mirrored).
- **All four BR shots of match 6 went wide right with their keeper near halfway**: the "centred keeper → corner" test only looked at its lateral position.
- **`carrier_shoot` now:** keeper more than 26 design units (~3 m) from its goal line → SHOOT CENTER, power 1.0, from up to 80 design units (~9.3 m, also from our half); otherwise as before (their half, angle check, CENTER/corners by keeper lateral). Replayed on six matches: SHOOT CENTER/1.0 66, BR 2, cut inside 6.
- Tests 166/166, `sim_match.py` 0 invalid / 0 incompatible, 5 × `test_local.py`.

## Match 7 (13:20, won 3-2; MID on Nova 2 Lite + empty-goal shooting live since ~12:42) → shot power, open play only
- `C:\dev\afc\captures\match7.log`. Two of our three goals were empty-goal CENTER shots (FWD2 from 3.2 m, DEF from ~6.9 m).
- **Over the bar:** an empty-goal CENTER/1.0 shot ended behind their goal line between the posts with no goal; the user saw many shots over the crossbar. Only 2 s snapshots exist, so trajectories are not visible.
- `carrier_shoot` also fired from the centre spot at kick-off/reset (keeper in goal, ball not in play).
- **Now:** power by distance to their line (≤ 30 design → 0.75, ≤ 55 → 0.85, beyond → 0.95) for every rule shot; the rule answers only in `OPEN_PLAY`.
- **Validator rejections (all fell back safely; no agent error or last resort):** DEF MARK with a distance as `target_player_id` (35, 32, 20, 33, 26) ×5; GK_DISTRIBUTE while a teammate held the ball ×4; GK SET_STANCE without `stance` ×2; FWD1 own-ball PRESS_BALL ×3; MOVE_TO beyond ±55 ×3 (GK, MID, FWD1). Not changed (each prompt line needs its own eval).
- Tests 168/168, `sim_match.py` 0 invalid / 0 incompatible.

## Match 8 (13:27, first match vs a human team, won 2-0; power fix not yet deployed) → FWD1 support
- `C:\dev\afc\captures\match8.log` (also holds match 7). Their keeper stayed in goal (x ≈ 6.0, never > 3 m out), so no empty-goal shots.
- Possession (inferred, open play): OURS 24, THEIRS 16, LOOSE 12, UNKNOWN 7; ball in their half 56 %.
- Rule shots 9 (FWD1 4, FWD2 3, MID 2), 2 goals: FWD1 BR from ~4.8 m, MID CENTER from ~6.9 m (keeper 0.49 m off centre).
- Stamina medians: ours GK 1, DEF 0.96, MID 0.37, **FWD1 0.29**, FWD2 0.44; theirs 0.35–0.74.
- **FWD1 pressed our own carrier** (own_ball rejected ×5; ×3 in match 7). New rule `fwd1_support` (FWD1 = `chain(carrier_shoot, fwd1_support)`): an outfield teammate holds our ball → MOVE_TO 15 design units ahead of the ball (capped at 0.6 × their goal x), lateral = ball lateral × 0.5, sprint only if > 15 away. On the 132 open-play states of matches 7–8 it answers 30 times.
- Other rejections, unchanged: GK_DISTRIBUTE while a teammate holds the ball ×6, DEF distance as target id ×3, DEF PASS without the ball ×2.
- Tests 171/171, `sim_match.py` 0 invalid / 0 incompatible.

## Matches 10 (14:03, lost 1-2) and 11 (14:10, lost 0-1) → always CENTER, sprint threshold 0.5
- `C:\dev\afc\captures\match11.log`; power-by-distance and `fwd1_support` were live (powers 0.95/0.85/0.75; FWD1 support 18×).
- Match 10: their keeper out of goal 90 % (median x -0.99); match 11: keeper always in goal (x ≈ 6.0). Their #4 scored in match 11 from 0.8 m after four attempts from 2–4 m.
- **38 rule shots in open play, matches 4–11:** 5 goals. CENTER 4/21, BR 1/13, BL 0/4. By distance 1/8 (≤ 4 m), 1/7 (4–5.5), 2/12 (5.5–7), 1/11 (> 7): no clear distance effect. Nearest opponent < 1 m 2/20, 1–2 m 2/12, ≥ 2 m 1/6. BL shots ended 2.8–3.6 m off centre (goal ≈ ±0.8 m).
- Match 11 open play: we sprinted on 29 % of outfield player-ticks vs their 19 % (idle 8 % vs 24 %); MID stamina hit 0 by mid-match. Stamina medians ours 0.22–0.31 vs theirs 0.44–1.0.
- **Now:** `carrier_shoot` always aims CENTER (power by distance unchanged); `SPRINT_MIN_STAMINA` 0.35 → 0.5.
- Tests 171/171, `sim_match.py` 0 invalid / 0 incompatible, 5 × `test_local.py`.

## Match 13 (14:40, our first match as team 1, lost 0-3) → team 1 plays a home view
- `C:\dev\afc\captures\match13.log` (also an earlier 14:33 match as team 0, won 1-0). Coordinates are NOT mirrored for team 1 (our GK at x ≈ +6, home scored at x ≈ +6.9).
- With our ball our forwards moved backwards: mean forward velocity FWD1 -0.40, FWD2 -0.06 m/s as team 1 vs +0.57/+0.37 as team 0. Rules gave correct team-1 targets (poacher x -38.5).
- **Cause: the models.** Replayed with Nova Lite on 10 of that match's states: as team 1, MOVE_TO went backwards for FWD2 7/8 (median -20 design units) and FWD1 4/5; the same states rotated 180° and presented as team 0: 2/8 and 2/4 (medians +1 / 0).
- **Fix (`lib/frame.to_home_view` / `from_home_view`, wired in `agent_base`):** for team 1 the design-frame state is rotated 180° (x, y → -x, -y; home/away, score and modeTeamId swapped) and the summary, model, rules, validator and fallback all run as team 0; replies are rotated back (MOVE_TO negated, FOLLOW_PLAYER target_team swapped, teamId = 1). The log line keeps the real team and adds `(home view)`.
  - Tests `afc_tests/test_home_view.py` (fixture `real_states_away_20261022.json`, 72 prompts): round trip, possession preserved, every rule gives the same physical command with and without the view, all five agents answer teamId 1 on the pitch. 180/180 offline.
  - Local Bedrock check through the full handler (Nova Lite, 10 away states): model MOVE_TOs forward +5.9 (FWD1), +3.5/+1.1/+1.5 (FWD2), +0.1 (DEF) m; none backwards.

## Match 14 (15:31, team 0, won 4-1) → empty goal: dribble closer before shooting
- `C:\dev\afc\captures\match14.log` (15-minute window: only this match). Home-view build live (no "(home view)" lines: we were team 0). No agent error or last resort; 9 validator rejections.
- Forwards moved forward with our ball (+0.2…+0.7 m/s). Stamina ours 0.40–0.63 vs theirs 0–0.40 (sprint limit 0.5 works). Their goal: a ~9.8 m shot at 15 m/s with our GK centred.
- Rule shots: 10, 2 goals. **Cumulative matches 4–14: 53 rule shots, 8 goals; CENTER 7/36, BR 1/13, BL 0/4; empty goal 4/24 vs keeper in goal 4/29.** Empty-goal shots from 8–10 m: 0/3 in match 14.
- **Now:** with an empty goal and more than 60 design units (~7 m) from their line, `carrier_shoot` dribbles 12 toward the goal (lateral halved, no sprint) and shoots from closer; ≤ 60 it shoots CENTER as before.
- Tests 181/181, `sim_match.py` 0 invalid / 0 incompatible, 5 × `test_local.py`.

## Match 15 (15:44, team 0, won 1-0) → GK distribution rule
- `C:\dev\afc\captures\match15.log`. The GK's model (prompt and fallback both say "THROW to nearest teammate") threw 3/3 distributions to DEF #1 inside our box (x ≈ -5.5) with opponents 0.2–0.6 m away; possession turned over each time, while FWD2 stood free near halfway (nearest opponent 1.7–2.6 m).
- **New rule `gk_distribute`** (GK = `chain(gk_loose_ball_in_box, gk_distribute)`): our GK holds the ball → GK_DISTRIBUTE to the most advanced open outfield teammate (no opponent within 13 design units ≈ 1.5 m); nobody open → the least marked one. KICK when the target is more than 40 design units (~4.7 m) away or nobody is open, else THROW.
  - The 3 real cases → KICK to #4. Replayed on all 26 open-play states with our GK on the ball (all matches, home view for team 1): #4 KICK 15, #3 KICK 3 / THROW 3, #2 KICK 1 / THROW 1, #1 THROW 2 / KICK 1 (only when #1 was open).
  - Tests `afc_tests/test_gk_distribution.py` (fixture `real_states_gk_distribution_20261022.json`). 191/191 offline, `sim_match.py` 0 invalid / 0 incompatible.

## Match 19 (2026-10-23 08:08, team 0, won 3-2) → own-half release + no backward carry (user request)
- `C:\dev\afc\captures\match19.log`. Our carrier in our own half: 13 ticks, 31 % moving backwards; decisions: `carrier_shoot` 6 (empty-goal dribbles out of our half with 4–5 opponents ahead), model PASS 5, model MOVE_TO 1. In 3 of 13 a teammate was free ahead.
- **User's policy:** in our half do not carry (never backwards); pass up quickly; with nobody free ahead and more than one opponent in front, shoot hard and to the top corner if possible.
- **New rule `own_half_release`** (first in the DEF/MID/FWD1/FWD2 chains): our outfield carrier in our own half in open play → PASS to the most advanced free teammate ahead (≥ 5 ahead, no opponent within 13; GROUND ≤ 35 design units, else AERIAL); none free and ≥ 2 OUTFIELD opponents in front → SHOOT power 1.0, TL/TR away from their keeper (CENTER if their goal is empty); otherwise the model.
  - Aim orientation from data: BL shots ended on the shooter's +lateral side for team 0, so L = +lateral as the shooter faces their goal; top corners themselves have no data yet (corners overall 1/17 vs CENTER 7/36).
  - Replayed on every own-half carry of all matches (145): PASS AERIAL 43, PASS GROUND 34, SHOOT CENTER 40 (empty goal), TL 21, TR 7. So about half of own-half possessions would end in a long shot; `RELEASE_MIN_OPPONENTS` is the knob.
- **New output filter `no_backward_carry`** (in `agent_base.send`, before the sprint limit): while I carry in our own half, a MOVE_TO more than 3 design units backwards becomes a 10-unit step forward (lateral × 0.7, no sprint). Other commands untouched.
- Tests `afc_tests/test_own_half.py` (11); `test_home_view.py` now checks the new chains for team 1 too (same physical command, TL/TR included). 202/202 offline, `sim_match.py` 0 invalid / 0 incompatible, 5 × `test_local.py`.

## 2026-10-23: far post from wide, home/away symmetry audit
- **User report:** from out wide, shots hit the side netting from both sides. If the server aims at a point inside the goal, from a tight angle the path crosses the line outside the near post. **`shot_aim`** (used by `carrier_shoot`): between the posts (|lateral| ≤ 7 design units ≈ 0.8 m) → CENTER; wider → the low FAR corner (shooter on his left → BR, on his right → BL; for team 0 +lateral is the shooter's left). Rule shots matches 4–19: wider than the posts 6/27 goals, between them 5/35.
- **Symmetry audit (`afc_tests/test_symmetry.py`):** real home states (match 1 open play + the GK-distribution fixture) rebuilt as the server would send them to team 1 (rotated 180°, home/away swapped), all five handlers wired as in `main.py`, with the rules/fallback and six fixed model replies (MOVE_TO, PRESS_BALL, FOLLOW_PLAYER, MARK, PASS, SHOOT): the model receives identical text and every reply is the same command rotated back. Found one difference: "-0.0" vs "0.0" in the summary for tiny coordinates → `state.summarize_state` now normalises negative zero.
- Static check: `pyflakes` over `lib/`, the five `main.py`, `afc_tools/`: no undefined names or unused imports (only an upstream cosmetic f-string in `lib/test_parsing.py`).
- `afc_tools/real_prompt_away.txt`: a real team-1 state (tick 3380, our MID on the ball) for `e2e_invoke.py --prompt-file` after a deploy.
- 213/213 offline, `sim_match.py` 0 invalid / 0 incompatible, 5 × `test_local.py`, parsing 12/12.

## Event account (Workshop Studio) — deployed 2026-10-22 ~11:20 UTC
- **Account `<EVENT_ACCOUNT_ID>`** (us-east-1), role `WSParticipantRole`, temporary credentials pasted into one PowerShell window (env vars, no profile).
- `agentcore/aws-targets.json` now points at this account. The personal account's `aws-targets.json` and `.cli/deployed-state.json` are in `C:\dev\afc\rollback\personal-<PERSONAL_ACCOUNT_ID>-state\`; restore both to manage that stack again.
- Bootstrap: plain `cdk bootstrap aws://<EVENT_ACCOUNT_ID>/us-east-1` (default `AdministratorAccess` execution policy; the scoped `AfcCfnExecAgentCore` policy exists only in the personal account).
- Deploy: same code as the personal account's 2026-10-22 deploy (identical zip fingerprints gk `f075e25d…`, def `fa8b798b…`, mid `1ffa5727…`, fwd1 `ed473cf7…`, fwd2 `4abc8b36…`).
- Checks: `e2e_invoke.py --invoke` 5/5 (`afc_tools/reports/e2e-20261022-132154.json`), `team_tick.py --invoke --reuse-sessions` 25/25 (`afc_tools/reports/team-20261022-132251.json`); DEF rule fired on our ball.
- Runtime ARNs to register:
  - GK `arn:aws:bedrock-agentcore:us-east-1:<EVENT_ACCOUNT_ID>:runtime/TeamStrandsBalanced_ai_gk_agent-hnD97P9xoY`
  - DEF `arn:aws:bedrock-agentcore:us-east-1:<EVENT_ACCOUNT_ID>:runtime/TeamStrandsBalanced_ai_def_agent-Df1N30CcJF`
  - MID `arn:aws:bedrock-agentcore:us-east-1:<EVENT_ACCOUNT_ID>:runtime/TeamStrandsBalanced_ai_mid_agent-UYvYPj4u3W`
  - FWD1 `arn:aws:bedrock-agentcore:us-east-1:<EVENT_ACCOUNT_ID>:runtime/TeamStrandsBalanced_ai_fwd1_agent-PgvVZp8f5j`
  - FWD2 `arn:aws:bedrock-agentcore:us-east-1:<EVENT_ACCOUNT_ID>:runtime/TeamStrandsBalanced_ai_fwd2_agent-C8otNs5OTo`
- **Laptop gotcha:** Windows has a 48 MB page file here; with Edge/Cursor open, the CDK build died with Node `Fatal process out of memory: Zone`. Close apps until ≥ 5 GB commit is free (`(Get-CimInstance Win32_OperatingSystem).FreeVirtualMemory`).

## Deployed 2026-10-18 ~21:10 (approved): forwards config + pending changes
- **Diff:** only the 5 code prefixes, with exactly the fingerprints listed below.
- **Stack:** `UPDATE_COMPLETE`. GK v4 (capture on), DEF/MID/FWD1/FWD2 v3, all `READY`.
- **Each runtime's S3 code prefix equals the prepared zip,** so FWD1 runs the Nova Lite `main.py`.
- **Checks after deploy:**
  - `e2e_invoke.py --invoke`: 5/5 valid (`afc_tools/reports/e2e-20261018-211223.json`). FWD1 on the sample state (ball at distOppGoal 41) now moves instead of shooting;
  - `team_tick.py --invoke --reuse-sessions`: 25/25 valid; warm ticks 0.80–0.93 s; GK rule 0.19 s (`afc_tools/reports/team-20261018-211256.json`).
- **The 3 fallbacks** were validator rejections with own-ball PRESS_BALL:
  - DEF 2× (one of them a 2-command reply with `duration −1`);
  - FWD1 1×, in `gk_holds_ball`, a scenario that was not among the forward evaluations' controls.
- **Rollback:** `C:\dev\afc\rollback\deployed-20261018\` (the previous version).

## Deployed 2026-10-22 ~11:00 UTC: DEF shape (prompt + rule)
- **Why:** a defender sliding and running wide toward the opponent's wing and corner. Measured first (`model_eval.py --position def --scenario-set defending`, Nova Lite, 8 situations × 4 reps, 96 calls, $0.0073; `afc_tools/reports/model-eval-def-20261022-124954.json`):

  | DEF prompt | Accepted | Own-ball rejections | Our-ball MOVE_TO | Past halfway | Chases into their half | Slides |
  |---|---|---|---|---|---|---|
  | live (pre) | 59 % | 13 | 1/16 | 0 | 2 | 0 |
  | spec as written (`def_shape_spec.json`) | 69 % | 10 | 5/16 | 1 | 0 | 0 |
  | spec + fixes (`def_shape.json`) | **81 %** | 6 | 10/16 | 7 | 1 | 0 |

  - No variant ever slid in these 8 situations; the slide restriction is a guardrail, not the measured effect.
  - On our ball the model mostly pressed our own carrier or tried to PASS without the ball (rejected → fallback MOVE_TO(-33, 0)), and ignored "never past halfway" in 7/10 moves (once it ran to the forward's x = 50).
- **What changed:**
  - `ai-def/src/main.py`: prompt = the `def_shape.json` variant, character-identical to the evaluated text (`afc_tests/fixtures/prompts/def_evaluated_shape.txt`); `rule_fn=def_shape_on_our_ball`.
  - `lib/rules.py`: `def_shape_on_our_ball`: an outfield teammate holds our ball → DEF `MOVE_TO` (y 0) 15 behind our most advanced outfield teammate, never past halfway; no model call. Their / loose / unknown ball, or our GK / DEF on the ball → the model as before.
  - `afc_tools/model_eval.py`: `--scenario-set defending` and DEF heuristics; `sim_match.py` wires the DEF rule like `main.py`.
  - `afc_tests/test_def_shape.py` (22 tests). Offline: 121/121, 5 × `test_local.py`, parsing 12/12, `sim_match.py` 0 invalid / 0 incompatible (DEF rule on 771 of 4,000 ticks).
- **Diff before deploy:** only the 5 code prefixes. Inside the zips, only `lib/rules.py` changed (all five) and `src/main.py` (DEF); no dependency changed.
- **Stack:** `UPDATE_COMPLETE`. GK v5 (capture on), DEF/MID/FWD1/FWD2 v4, all `READY`, same ARNs. Prefixes: gk `f075e25d5fdb2e40…`, def `fa8b798b835cf047…`, mid `1ffa57270d926d99…`, fwd1 `ed473cf7c6a7f52f…`, fwd2 `4abc8b36877ea13c…`.
- **Checks after deploy:** `e2e_invoke.py --invoke` 5/5 valid (`afc_tools/reports/e2e-20261022-130244.json`); `team_tick.py --invoke --reuse-sessions` 25/25 valid (`afc_tools/reports/team-20261022-130309.json`): DEF answered `fwd1_on_ball_attack` by rule (MOVE_TO 0, 0) and `gk_holds_ball` with a valid model MOVE_TO(-10, 0) (was an own-ball PRESS_BALL rejection on 10-18).
- **Not changed, still open:** on their ball 3–4 of our players go to the ball (`team warning` in `opponent_attack` / `sample_as_away_team`), as before.
- **Rollback:** `C:\dev\afc\rollback\deployed-20261018-2110\` (README + MANIFEST; the live version before this deploy).

## Prepared deployment 2026-10-18 (synthesized with --dry-run, then deployed as above)
- **Forwards:**
  - FWD1: Nova Micro → **Nova Lite**, prompt = C + ids, i.e. `shoot_threshold_25_ids.json` applied with `apply_variant` to the previous source;
  - FWD2: Nova Lite, prompt = C (`shoot_threshold_25.json`);
  - both rendered prompts are character-identical to the evaluated texts (`afc_tests/test_forward_config.py`, fixtures in `afc_tests/fixtures/prompts/`).
- **Also included** (pending changes, below): INTERCEPT own-ball rejection, the `playerId` line in all five prompts, the `AFC_PROTOCOL` warning, inert `teammate_detail` code. V2 stays off.
- **Synth vs deployed:**
  - template: only the 5 code S3 prefixes differ;
  - each zip: exactly `src/main.py`, `lib/action_schema.py`, `lib/action_validator.py`, `lib/agent_base.py` and `lib/state.py` changed; no file added or removed; dependencies identical;
  - GK keeps `AFC_CAPTURE_STATE=1`; no `PlatformVersion`.
- **Expected new fingerprints (S3 prefixes) in the deploy diff:** gk `51aa3031e308a0d9…`, def `d7996a4a6bbe56c1…`, mid `5f3a025bbacfc757…`, fwd1 `bc39b1cff15844b9…`, fwd2 `5832f24faa0eb539…`.
- **Rollback:** `C:\dev\afc\rollback\deployed-20261018\` (README + MANIFEST). The CDK fingerprint of each copied zip equals the deployed S3 key.
- **Tool change:** `model_eval.py --base-prompt` reproduces the earlier experiments from `afc_tests/fixtures/prompts/*_pre_20261018.txt`; the variant files no longer apply to the changed `main.py`.

## Pending local changes (tested offline, NOT deployed)
Deployed: GK v3, others v2 (2026-10-18 19:1x). Not deployed yet:
| Change | Evidence | Measured |
|---|---|---|
| INTERCEPT rejected on our own ball (`own_ball`), like PRESS_BALL | consistency; no real case seen yet | lab parity: 8 verdicts flip, all INTERCEPT while OURS; 72 already-rejected replies gain the issue; summaries/fallbacks unchanged |
| Prompt line in all five: `"playerId" is always N (you). Every parameter goes inside "parameters"; …` | all 3 FWD1 rejections on 2026-10-18 = FOLLOW_PLAYER with the target's id in playerId and flat params | effect on the model: `model_eval.py --variants current,deployed` (paid, not run yet) |
| `AFC_PROTOCOL` warning (key names only) when the payload lacks gameState/teamId/myPlayers; counted by `match_watch.py` | match day: unknown envelope | 4 offline cases, values never logged |
| Opt-in `AFC_PLATFORM_VERSION` (CDK property override in `agentcore/cdk/lib/cdk-stack.ts`) | V2 runtime: ~2 s cold starts vs 5.4–30 s (AWS blog, 2026-09-18); our runtimes are `V1` | synth: unset = deployed template (0 differences besides zip prefixes); `V2` = only `PlatformVersion: V2` on the 5 runtimes. **Untested with CodeZip.** `PlatformVersion` is not create-only, so it updates in place and the ARNs stay |
Tests: 47 offline (40 → 47), 5 × `test_local.py`, parsing 12/12, `sim_match.py` 0 invalid / 0 incompatible.

## Measurements 2026-10-18 (session 2)
- **Simulation** (`sim_match.py`, toy physics, 20 × 200 ticks, 20,000 handler calls, 12 s):
  - 0 invalid, 0 incompatible;
  - possession inference disagrees with the truth on 13.2 % of ticks (OURS → UNKNOWN 3.1 %, THEIRS → UNKNOWN 10.1 %: the same-number opponent within `MIN_SEPARATION`). This depends on the invented dribble offset, so calibrate on real captures before changing thresholds;
  - rule situations: loose ball in our box 6.3 % (GK rule fired 253/253), GK on the ball 2.4 %, opponent on the ball near our goal 7.7 %, our outfield carrier within 25 of goal 0.6 %.
- **Latency budget:**
  - cold call = AgentCore start-up ≈ 5–7 s, plus our process start 1.0–1.3 s (import: strands 0.21 s, opentelemetry 0.17 s self time), plus the handler;
  - warm handler 0.4–0.9 s (model);
  - our code without the model: 0.17 ms per call;
  - so only the model (or a rule: 0.19 s warm) and the platform version can move the needle.
- **Logs:**
  - 16–24 KB per call; ~70 % is the OTEL copy of the same lines;
  - a GK capture adds ~9.7 KB per call (each capture is written in 3 formats);
  - retention: never expire.
- **Sessions:**
  - `idleRuntimeSessionTimeout` 900 s, `maxLifetime` 28,800 s on all five;
  - our test sessions close after 15 idle minutes;
  - per AWS pricing, CPU is not billed during I/O wait, but memory is billed while a session lives (V2 reclaims idle memory).
- **OpenTelemetry 400:** span export needs CloudWatch Transaction Search (account setting, not changed).

## FWD1 model evaluation 2026-10-18 (`model_eval.py`, local Bedrock calls, nothing deployed)
Run: `--position fwd1 --models micro,lite --variants current,deployed --reps 3 --seed 1`.
- 108/108 calls, exactly 108 HTTP requests (no retries), 0 API errors, estimated $0.0061 (Price List rates, not a bill).
- Rows: `afc_tools/reports/model-eval-fwd1-20261018-200624.jsonl`; summary in the matching `.json`.

| Config | Parsed | Accepted | Protocol errors | Bedrock p50 | Wall p50 / p95 | Tokens in / out | Est. $ per 1k calls |
|---|---|---|---|---|---|---|---|
| micro / deployed | 27/27 | 26/27 | 1 | 0.42 s | 0.53 / 0.62 s | 995 / 36 | 0.039 |
| micro / current | 27/27 | 26/27 | 1 | 0.43 s | 0.54 / 0.74 s | 1031 / 40 | 0.041 |
| lite / deployed | 27/27 | 27/27 | 0 | 0.53 s | 0.66 / 1.05 s | 995 / 64 | 0.072 |
| lite / current | 27/27 | 27/27 | 0 | 0.51 s | 0.68 / 0.91 s | 1031 / 62 | 0.074 |

- **Objective:**
  - acceptance: Micro 52/54 (95 % CI 87–99 %) vs Lite 54/54 (93–100 %); the intervals overlap;
  - both Micro errors were FOLLOW_PLAYER with a wrong `playerId` and flat parameters. Micro used FOLLOW_PLAYER 6×, Lite never;
  - the new prompt line made no measurable difference (1 vs 1 Micro errors).
- **Heuristic (our judgement):**
  - with a teammate on the ball, Micro stayed put 6/17 or ran onto the carrier 5/17; Lite did neither (0/18) and made forward support runs 9/18 (Micro 7/17);
  - all four configs SHOOT from 41.3 units in 12/12 (`fwd1_on_ball_attack`); in range (19.4 units) all SHOOT 12/12;
  - Lite sprints on 26/27 MOVE_TO (stamina cost unknown on the server);
  - Lite PRESS from ~56 units away on a loose ball in our box 2/6.
- **Recommendation (not applied; it needs a model change in `ai-fwd1/src/main.py` and a deploy):**
  - FWD1 → Nova Lite with the current prompt. The evidence is protocol robustness plus support-play behaviour, at about +0.1 s latency;
  - long shots are not fixed by either model → prompt experiment next.

## Shooting prompt experiment 2026-10-18 (forwards, current models, nothing deployed)
Reproduce (one run per forward, 60 calls each, hard cap 60):
```powershell
$vf = "shoot25=afc_tools\prompt_variants\shoot_threshold_25.json"
python afc_tools\model_eval.py --position fwd1 --models micro --variants deployed,current,shoot25 --variant-file $vf --scenario-set shooting --reps 4 --seed 1 --max-calls 60 --invoke
python afc_tools\model_eval.py --position fwd2 --models lite  --variants deployed,current,shoot25 --variant-file $vf --scenario-set shooting --reps 4 --seed 1 --max-calls 60 --invoke
```
- Rows: `afc_tools/reports/model-eval-fwd1-20261018-202448.jsonl` and `model-eval-fwd2-20261018-202530.jsonl` (+ `.json` summaries with the exact prompts and summaries).
- 120/120 calls = 120 HTTP requests, estimated $0.0071; 1 reply without usage metadata, charged at the estimate.
- Variants: A = deployed, B = current repo (A + playerId line), C = `shoot25` (B with the "~25 units" line replaced by an explicit `distOppGoal <= 25` rule and what to do above it).
- Scenarios: `--scenario-set shooting`, i.e. 5 carrier situations, FWD2's mirrored.

| Forward / variant | Accepted | Far shots (distOppGoal > 25) | In-range shots | Pass to better-placed teammate | Carrier advances (≥ 5 closer) | Bedrock p50 | Tokens in / out |
|---|---|---|---|---|---|---|---|
| FWD1 Micro A | 20/20 | 16/16 | 4/4 | 0/4 | 0 | 391 ms | 994 / 33 |
| FWD1 Micro B | 20/20 | 16/16 | 4/4 | 0/4 | 0 | 397 ms | 1030 / 33 |
| FWD1 Micro C | 20/20 | **8/16** (rule broken 8×, incl. 4/4 under pressure) | 4/4 | 0/4 | 0 | 436 ms | 1107 / 36 |
| FWD2 Lite A | 20/20 | 16/16 | 4/4 | 0/4 | 0 | 534 ms | 1008 / 57 |
| FWD2 Lite B | 20/20 | 16/16 | 4/4 | 0/4 | 0 | 514 ms | 1044 / 60 |
| FWD2 Lite C | 20/20 | **0/16** | 4/4 | 0/4 | 9 | 550 ms | 1121 / 61 |

- **Heuristic** — what replaced the far shots:
  - **FWD1 Micro C:** 5 of 8 were MOVE_TO onto its own spot (standing still), 1 a 35-unit retreat, 1 a backward pass to DEF; no real advance.
  - **FWD2 Lite C:** advances of +5…+28 in x;
    - under pressure, 4/4 passes to DEF #1, 35 units behind, when MID was ~12 away. FWD2's prompt already says "PASS to Forward 1 or Midfielder if you're under pressure";
    - with a free FWD1 13 units from goal, FWD2 dribbled toward him (0/4 passes).
- **Reading:**
  - C fixes long shots for Lite without losing in-range shots; for Micro it halves them, but replaces them mostly with standing still;
  - pass targeting is the remaining weakness;
  - A and B are indistinguishable.
- **Not established:**
  - whether 25 is the right range on the server, and whether MOVE_TO while holding the ball dribbles;
  - FWD1 on Lite with C (not run).

## Passing prompt experiment 2026-10-18 (forwards on Nova Lite, nothing deployed)
Reproduce (84 + 56 calls, hard caps):
```powershell
$vf1 = "shoot25=afc_tools\prompt_variants\shoot_threshold_25.json"; $vf2 = "shoot25pass=afc_tools\prompt_variants\shoot_threshold_25_pass.json"
python afc_tools\model_eval.py --position fwd1 --models lite --variants current,shoot25,shoot25pass --variant-file $vf1 --variant-file $vf2 --scenario-set shooting_control --reps 4 --seed 1 --max-calls 84 --invoke
python afc_tools\model_eval.py --position fwd2 --models lite --variants shoot25,shoot25pass --variant-file $vf1 --variant-file $vf2 --scenario-set shooting_control --reps 4 --seed 1 --max-calls 56 --invoke
```
- Rows: `afc_tools/reports/model-eval-fwd1-20261018-203124.jsonl`, `model-eval-fwd2-20261018-203230.jsonl`.
- 140/140 calls = 140 HTTP requests, estimated $0.0116; 1 reply without usage metadata.
- Scenarios: `shooting_control` = the 5 carrier situations + 2 off-ball controls (`mid_on_ball_midfield`, `opponent_attack`).
- **What the model can see:** a forward's summary has its own distOppGoal, everyone's (x, y), opponents' distToMe and the goal x. It does **not** have teammates' distance to goal or who marks whom (only MID gets teammates' distOppGoal). C2's "better placed / free teammate" therefore has to be inferred from coordinates.

| Forward · variant | Accepted | Far shots | In-range shots | Pass to better teammate | Passes (backward / to GK-DEF) | Under pressure | Carrier advances (clear) | Off-ball | Tokens in |
|---|---|---|---|---|---|---|---|---|---|
| FWD1 · B | 28/28 | 16/16 | 4/4 | 0/4 | 0 (0/0) | SHOOT 4/4 | 0 | ok | 1030 |
| FWD1 · C | 28/28 | **3/16** | 4/4 | **1/4** | 4 (3/2) | SHOOT 3/4, back-pass 1 | 4 (4) | ok | 1107 |
| FWD1 · C2 | **25/28** | 3/16 | 4/4 | 0/4 | 2 (2/0) | SHOOT 3/4, back-pass 1 | 10 (10) | **3/4 PRESS_BALL on our own ball (rejected)** | 1222 |
| FWD2 · C | 28/28 | **0/16** | 4/4 | 0/4 | 6 (**6/6**) | back-pass to DEF 3/4 | 10 (10) | ok | 1121 |
| FWD2 · C2 | **26/28** | 3/16 | 4/4 | 0/4 | 6 (5/2) | SHOOT 3/4 | 4 (4) | **2/4 PRESS_BALL on our own ball (rejected)** | 1236 |

- **C2 vs C:**
  - C2 did not reach its goals: passes to the better-placed teammate went 1/8 → 0/8 across both forwards, and forward or level passes under pressure stayed at 0/8;
  - it added an off-ball regression (5/16 own-ball PRESS_BALL, caught by the validator) and brought back far shots for FWD2 (3, all under pressure);
  - its only gain is fewer passes to the GK/DEF.
- **Choice:**
  - C for both forwards, FWD1 on Lite. Far shots: 16/16 → 3/16 (FWD1) and 0/16 in two separate runs (FWD2), in-range shooting intact;
  - remaining problems: shots under pressure (FWD1) and backward passes to DEF (FWD2).
- **Variance:** FWD2 · C `advance_open_space` was 3 MOVE + 1 PASS in the previous run and 1 MOVE + 3 PASS now. Pass and advance counts are descriptive only.

## Enriched teammate state experiment 2026-10-18 (forwards, Lite + C, nothing deployed)
- **Code:** `lib/state.summarize_state(..., teammate_detail=False)`. When True, every teammate line gets `distOppGoal` (same `|x − goal x|` as the player's own line) and `nearestOpp` (euclidean, like `distBall`); either reads `unknown` if it can't be computed from real numbers.
  - **Off by default and not wired** into the handler or any runtime. Off, 6,200 summaries (620 states × 5 positions × 2 teams) are byte-identical to before.
  - **Pre-existing, unchanged:** a `null` or string coordinate makes the original summary lines raise; the handler then plays that tick on the fallback.
- **Reproduce:** `model_eval.py --position fwdN --models lite --variants shoot25,shoot25_rich --variant-file shoot25=… --enriched shoot25_rich=shoot25 --scenario-set shooting_control --reps 4 --seed 1 --max-calls 56 --invoke`.
  - Rows: `afc_tools/reports/model-eval-fwd1-20261018-204129.jsonl`, `model-eval-fwd2-20261018-204237.jsonl`.
  - 112/112 calls = 112 HTTP requests, estimated $0.0093, no missing usage.

| Forward · state | Far shots | In-range | Pass to better teammate | Passes (backward / to GK-DEF) | Under pressure | Advances | Off-ball | Bedrock p50 | Wall p95 | Tokens in |
|---|---|---|---|---|---|---|---|---|---|---|
| FWD1 · current | 0/16 | 4/4 | 1/4 | 5 (3/3) | PASS→DEF 3, MOVE 1 | 7 | ok | 501 ms | 0.82 s | 1107 |
| FWD1 · enriched | **4/16** | 4/4 | 1/4 | 2 (0/0) | **SHOOT 4/4** | 5 | ok | 552 ms | 0.85 s | 1179 |
| FWD2 · current | 0/16 | 4/4 | 0/4 | 4 (4/4) | PASS→DEF 4/4 | 9 | ok | 490 ms | 1.11 s | 1121 |
| FWD2 · enriched | 1/16 | 4/4 | 0/4 | 2 (2/2) | PASS→DEF 2, SHOOT 1, MOVE 1 | 11 | ok | 529 ms | 0.99 s | 1193 |

- **Verdict:** not promising. Passes to the better-placed teammate did not improve (FWD1 1/4 vs 1/4, FWD2 0/4 vs 0/4). FWD1's backward passes under pressure turned into far shots. Keep the current state.
- **Why the passing fails** (pooled C replies, all Lite runs):
  - **Representation (strong evidence):** the forwards' prompts never map ids to roles, and the summary labels teammates `P1…P4` (P1 is DEF). FWD2's prompt says "PASS to Forward 1 or Midfielder if you're under pressure". **All 15 of FWD2's passes in 76 replies went to #1 (DEF)**, never to #3 (the real Forward 1) or #2 (MID).
  - **Reasoning:** with a free, better-placed partner, FWD1 ran onto the partner's exact spot 6/12 instead of passing (2/12 passes).
  - **Scenario:** in `pass_better_teammate` the partner's nearest opponent is 7.1 away and the opponent GK 10, so "no opponent near him" is arguable. `nearestOpp` also makes a deep defender look "free" (25), which is not the same as a useful pass.

## Id-to-role mapping experiment 2026-10-18 (option a, forwards on Lite, nothing deployed)
- **Variant:** `shoot25ids` (`afc_tools/prompt_variants/shoot_threshold_25_ids.json`) = C plus a "## Player ids" section (0 GK, 1 DEF, 2 MID, 3 Forward 1, 4 Forward 2, and how the summary names them). It differs from C only by that section (tested).
- **Rows:** `afc_tools/reports/model-eval-fwd1-20261018-204731.jsonl`, `model-eval-fwd2-20261018-204818.jsonl`.
- 112/112 calls = 112 HTTP requests, estimated $0.0095; 1 reply without usage metadata.

| Forward · variant | Far shots | In-range | Pass targets | Under pressure (4) | Pass to better teammate | Advances | Off-ball | Bedrock p50 | Wall p50 / p95 | Tokens in |
|---|---|---|---|---|---|---|---|---|---|---|
| FWD1 · C | 2/16 | 4/4 | DEF ×3, FWD2 ×2 | SHOOT 2, PASS→DEF 2 | 1/4 | 4 | ok | 479 ms | 0.59 / 0.84 s | 1107 |
| FWD1 · C + ids | **0/16** | 4/4 | **FWD2 ×4** | **PASS→FWD2 3**, MOVE 1 | 0/4 | 6 | ok | 518 ms | 0.69 / 0.93 s | 1212 |
| FWD2 · C | 0/16 | 4/4 | DEF ×4 | PASS→DEF 3, MOVE 1 | 0/4 | 7 | ok | 488 ms | 0.60 / 0.78 s | 1121 |
| FWD2 · C + ids | **3/16** | 4/4 | none | **SHOOT 3**, MOVE 1 | 0/4 | 9 | ok | 497 ms | 0.62 / 0.76 s | 1226 |

- **FWD1:** the mapping removed every pass to DEF, and under pressure it now passes to FWD2 (its x is 5 behind, so it counts as "backward" by our −2 rule; FWD2 is the free forward). No far shots; no off-ball regression.
- **FWD2:**
  - the mapping removed the passes to DEF, but it never passes to Forward 1 (#3) or MID (#2), and under pressure it shoots from 30 instead (3/4). It fails the success criterion;
  - in the under-pressure layout both FWD1 and MID are behind FWD2 (x 20 and 15 vs 25), and C says "pass to the nearest free teammate"; it shoots rather than pass backwards.
- **Confirms:**
  - the id/role confusion was real (FWD1: DEF passes 3 → 0; FWD2: 3 → 0);
  - removing a bad option exposes the next weakness, shooting under pressure, which no prompt variant has fixed reliably yet.

## FWD2 pressure-rule experiment 2026-10-18 (Lite, nothing deployed)
- **Variant B:** `shoot_threshold_25_ids_fwd2_pressure.json` = C + the id section + an explicit under-pressure rule (Forward 1 id 3 or MID id 2, even if slightly behind, when no opponent is close to them; otherwise MOVE_TO; no SHOOT above 25).
  - It replaces C's "nearest free teammate" clause and FWD2's original pressure line, so no two instructions disagree.
  - Ids are given as "id 3", with the target in `parameters.target_player_id`, so it never suggests a different `playerId`.
- **Rows:** `afc_tools/reports/model-eval-fwd2-20261018-205239.jsonl`. 56/56 calls = 56 HTTP requests, estimated $0.0049.

| FWD2 · variant | Accepted | Protocol errors | Far shots | In-range | Under pressure (4) | Pass targets | Advances | Off-ball | Bedrock / wall p50 / p95 | Tokens in |
|---|---|---|---|---|---|---|---|---|---|---|
| A · C | 28/28 | 0 | 0/16 | 4/4 | PASS→DEF 4 | DEF ×4 | 8 | ok | 0.65 / 0.80 s | 1121 |
| B · C + ids + pressure rule | **24/28** | **2** (`duration: -1`) | 1/16 | 4/4 | PASS→FWD1 2, MOVE 1, SHOOT 1 | FWD1 ×2 | 8 (2 run onto FWD1's spot) | **2 own-ball PRESS_BALL + 2 duration −1 (rejected)** | 0.61 / 0.99 s | 1304 |

- **B's passes under pressure are reasonable:** FWD1 is free, with its nearest opponent 16.8 away, and 5 units behind.
- **B fails the success criteria:** protocol errors 0 → 2, an off-ball regression in 4/8 off-ball replies, and one shot from 30 remains.
- **Keep C for FWD2.**
- **Pattern across experiments:** the longer the prompt, the more Lite slips in situations the change was not about. Off-ball own-ball PRESS_BALL or `duration −1`:

  | Variant | Extra prompt chars over C | Off-ball slips |
  |---|---|---|
  | C, C + ids | — / +244 | 0 |
  | C2 | +384 | 5 / 16 |
  | B | +529 | 4 / 8 |

  Further tactical fixes should prefer data in the summary or validator/rule changes over more prompt text.
- **Needed before more pressure tuning:**
  - several pressure layouts (teammates ahead / level / behind, different opponent counts) with ≥ 8 reps each, instead of one layout × 4;
  - real captures showing how often pressure happens and what passes are possible;
  - server semantics: interception of short back passes, shot success from about 30 units.

## Match day (no organiser contact before the event)
See also `AFC_MATCHDAY_CHECKLIST.md` (Spanish, step by step).
Nothing is known yet about how the server calls the team. Find out from our own logs, read-only:
```powershell
$env:AWS_PROFILE = "afc-dev"; $env:AWS_REGION = "us-east-1"
python afc_tools\match_watch.py --minutes 10 --team <our team id, once known>
```
1. **Before the first match:** `python afc_tools\e2e_invoke.py --invoke` (5 paid calls). All five must be valid. It also confirms credentials, region and permissions on the day.
2. **First minutes of the first match:** run `match_watch.py` and read the following:
   - **sessions:**
     - `reused` → warm ticks of about 1 s, nothing to do;
     - `new session per call` → every call is cold, 7–9 s, mostly AgentCore start-up. Only ~1 s of that is ours (Python + OTEL imports), so there is no quick fix on our side. Tell the organisers.
   - **asked (team, player):** each runtime must be asked for its own player (GK 0 … FWD2 4). `UNEXPECTED` means the server's `myPlayers`/`teamId` differ from what the agents assume. Read the GK's `LAB_CAPTURE` lines before changing anything.
   - **sources:** mostly `model`, plus some `validator_fallback`. Any `error_fallback` or `last_resort` → read that runtime's log group.
   - **interval:** the real tick period. Compare it with the `handler` time.
3. **Calibration from real states:** export the GK log, then `lab\experiment_7\run.py extract <log> <dir>` and `inspect`. The extractor writes each capture twice (the runtime logs it in two parseable formats); de-duplicate by hash. Check possession (`CONTROL_RADIUS`/`MIN_SEPARATION`, team fields), stamina range, playMode, coordinates and box size against real data.
4. **After the event:** switch capture off (see below). For cleanup, see the cleanup section and the identity plan.

## Capture switch
- **Locally:** `$env:AFC_CAPTURE_STATE = "1"` before running.
- **Deployed:** add to one runtime in `agentcore/agentcore.json`
  `"envVars": [{"name": "AFC_CAPTURE_STATE", "value": "1"}]`
  then redeploy; remove it and redeploy to switch off.
- **Then:** download that runtime's log and run `lab\experiment_7\run.py extract <log> captures\` in the OneDrive lab.
- **Cost:** each line is the full game state (~2.6 KB) per invocation, which adds to CloudWatch Logs ingestion. Enable it for a few minutes only.

## Costs to watch (not all may be covered by event credits; check Billing → Credits)
- **Bedrock tokens:** 5 agents × invocations per match. The tick rate is unknown.
  - Input per call starts at about 700 tokens and grows to about 4,200 once Strands' 40-message history fills (lab Experiment 4).
  - `ai-mid` uses **Nova Pro**, by far the most expensive of the three.
- **AgentCore Runtime:** consumption-based while sessions run.
- **Others:** CloudWatch Logs ingestion and storage, X-Ray traces (the OTEL distro is in each package), and S3 storage for the code zips.
- **Recommended:** an AWS Budget alert before the first match (creating it is a resource change; needs approval).

## Cleanup
**Decided 2026-10-22: remove the PERSONAL-account deployment (<PERSONAL_ACCOUNT_ID>) after the event (22–23 Oct).** The event account (<EVENT_ACCOUNT_ID>) plays the matches.
- `python afc_tools\cleanup_personal.py` (plan, read-only; tested 2026-10-22: 5 runtimes + 5 roles + 5 policies), then `--delete`. It uses the `afc-dev` profile, assumes the CDK deploy role and deletes the stack with the cfn-exec role, like `cdk destroy`. It refuses any account other than <PERSONAL_ACCOUNT_ID>, so the event account is never touched.
- Afterwards, optional in the console: the log groups `/aws/bedrock-agentcore/runtimes/*` and `CDKToolkit` in <PERSONAL_ACCOUNT_ID>; then checklist §7 (deactivate `afc-dev` keys, etc.).
- Do **not** use the `agentcore remove` steps below for the personal account now: `aws-targets.json`/`deployed-state.json` point at the event account (personal copies in `C:\dev\afc\rollback\personal-<PERSONAL_ACCOUNT_ID>-state\`).

1. `agentcore remove agent --name ai_gk_agent`, and the same for `ai_def_agent`, `ai_mid_agent`, `ai_fwd1_agent`, `ai_fwd2_agent`.
2. Then `python ..\afc_tools\with_lib.py agentcore deploy`, which applies the removal.
3. Alternatively, delete the stack `AgentCore-TeamStrandsBalanced-default` in CloudFormation.
4. Afterwards, delete the log groups `/aws/bedrock-agentcore/runtimes/*` if they are not wanted.
5. Keep `CDKToolkit` if you will deploy again. Deleting it means emptying its S3 bucket and ECR repository first.
