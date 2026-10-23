"""Controlled model / prompt comparison for one position, through our real summary and validator.

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\model_eval.py --position fwd1                 # plan: calls, tokens, cost
    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\model_eval.py --position fwd1 --invoke        # PAID: Bedrock calls

What is compared: each (model, prompt variant) answers the SAME scenarios the SAME number of
times, in a seeded random order (so no combination always runs first or at the same moment).
  models     --models micro,lite,pro (Nova inference profiles us.amazon.nova-*-v1:0)
  variants   current  = the position's SYSTEM_PROMPT as it is now in ai-<pos>/src/main.py
             deployed = current minus PROMPT_LINES_ADDED (the lines added after the 2026-10-18
                        deployment); checked equal to the prompt inside the deployed code zip
How each call runs, like the runtime: Strands Agent, history window 0, default model parameters
(no temperature/max_tokens set, as in lib/agent_base.create_agent), the scenario's
lib/state.summarize_state(), then lib/action_validator on the raw reply. One Bedrock client per
model, reused. The call goes from this machine to Bedrock in --region (us-east-1): latency
includes our network path; comparable between configurations, not equal to the runtime's.
No retries anywhere (Strands ModelRetryStrategy(max_attempts=1), botocore total_max_attempts=1), every
HTTP request is counted, and the run stops when requests exceed the plan, the estimated cost
exceeds --max-usd, or 3 calls in a row fail. Nothing deployed is touched (no AgentCore call).

Output (never overwritten): reports/model-eval-<pos>-<time>.jsonl, one row per call as it
happens, and reports/model-eval-<pos>-<time>.json with the summary.

Metrics. OBJECTIVE: JSON parsed, accepted by the validator, rejection codes (protocol vs
possession), commands, latency, tokens, estimated cost. HEURISTIC (our football judgement, not
a quality score, reported as counts per scenario):
  long_shot           SHOOT from > LONG_SHOT units (the prompts say "~25")
  better_teammate     SHOOT while a teammate is >= 10 units closer to goal with no opponent within 5
  support_run         a teammate holds the ball and I MOVE_TO >= 5 units toward the opponent goal
  defend_own_ball     MARK/SLIDE_TACKLE/PRESS_BALL/INTERCEPT while our team holds the ball
Scenarios (test data derived from sample_prompt.txt, not captures):
  --scenario-set default   afc_tools/team_tick.py's five plus four built around the ball carrier
  --scenario-set shooting  SHOOTING_LAYOUTS, for FWD1 and (mirrored) FWD2 with the ball: long range,
                           in range, better-placed teammate, under pressure, open space ahead
Summary variants: --enriched NAME=BASE runs BASE's prompt with lib/state.summarize_state(...,
teammate_detail=True), i.e. teammates' distOppGoal and nearestOpp added; nothing else changes.
Extra prompt variants: --variant-file NAME=PATH (see afc_tools/prompt_variants/*.json; each
"old" text must occur exactly once in the current prompt). --max-calls refuses a bigger plan.
Carrier metrics (shooting set): shots with distOppGoal above / within SHOT_THRESHOLD, shoot rate
far vs near, PASS to a better-placed teammate, carrier advances (MOVE_TO >= 5 closer to goal).
"""

import argparse
import copy
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEAM = os.path.join(ROOT, "ai-team-strands-balanced")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)
from test_helpers import mock_agentcore  # noqa: E402

mock_agentcore()
from action_validator import context_from_state, extract_reply, validate_reply  # noqa: E402
from fallback import DEF_CONFIG, FWD1_CONFIG, FWD2_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback  # noqa: E402
from possession import Possession, resolve_possession  # noqa: E402
from state import get_goal_positions, summarize_state  # noqa: E402
from team_tick import _player, scenarios as tick_scenarios  # noqa: E402

POSITIONS = {"gk": (0, "GK", GK_CONFIG), "def": (1, "DEF", DEF_CONFIG), "mid": (2, "MID", MID_CONFIG),
             "fwd1": (3, "FWD1", FWD1_CONFIG), "fwd2": (4, "FWD2", FWD2_CONFIG)}
MODELS = {"micro": "us.amazon.nova-micro-v1:0", "lite": "us.amazon.nova-lite-v1:0", "pro": "us.amazon.nova-pro-v1:0",
          # 2026-10-22 candidates (inference profiles listed by bedrock:ListInferenceProfiles in us-east-1)
          "nova2lite": "us.amazon.nova-2-lite-v1:0",
          "haiku45": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
          "haiku55": "us.anthropic.claude-haiku-5-5",
          "sonnet55": "us.anthropic.claude-sonnet-5-5",
          "sonnet55low": "us.anthropic.claude-sonnet-5-5"}
# Extra Converse fields per model: Claude models think by default, which costs latency; turn it off.
# Confirm with afc_tools/model_probe.py --invoke that the account accepts them before a run.
MODEL_FIELDS = {"haiku55": {"thinking": {"type": "disabled"}},
                "sonnet55": {"thinking": {"type": "between_tools"}},
                # model_probe 2026-10-22 (event account): effort low answered in ~0.9 s vs 1.1-1.4 s with thinking off
                "sonnet55low": {"output_config": {"effort": "low"}}}
# USD per 1M tokens (input, output): AWS Price List API, AmazonBedrock us-east-1, publication
# 2026-10-16, on-demand "USE1-Nova{Micro,Lite,Pro}-{input,output}-tokens". Estimates, not bills;
# the rate applied to the us.* cross-region profiles was not confirmed.
PRICE = {"micro": (0.035, 0.14), "lite": (0.06, 0.24), "pro": (0.80, 3.20),
         # Claude: Anthropic first-party list prices (claude-api skill, cached 2026-10-06); Bedrock is priced
         # separately and may differ. nova2lite: UNKNOWN - a deliberately high placeholder for the budget guard.
         "haiku45": (1.00, 5.00), "haiku55": (0.10, 0.50), "sonnet55": (2.00, 10.00), "sonnet55low": (2.00, 10.00), "nova2lite": (1.00, 5.00)}
PROMPT_LINES_ADDED = ['"playerId" is always {pid} (you). Every parameter goes inside "parameters"; '
                      'the player you pass to, mark or follow is parameters.target_player_id.\n']
LONG_SHOT = 35.0
SHOT_THRESHOLD = 25.0   # the value under test in the shooting experiment, measured as the summary's distOppGoal (|x - goal x|)
BETTER_MARGIN = 10.0    # a teammate this much closer to goal (euclidean) with no opponent within 5 is "better placed"
PRESSURE_RADIUS = 2.5   # opponents this close to me count as pressure
POSSESSION_CODES = {"needs_ball", "needs_opponent_ball", "own_ball"}
DEFENSIVE = {"MARK", "SLIDE_TACKLE", "PRESS_BALL", "INTERCEPT"}
VARIANTS_DIR = os.path.join(HERE, "prompt_variants")


# --- what is evaluated ------------------------------------------------------------------------

def load_prompt(position):
    """The position's rendered SYSTEM_PROMPT, imported from its main.py (no model is called)."""
    path = os.path.join(TEAM, f"ai-{position}", "src", "main.py")
    spec = importlib.util.spec_from_file_location(f"afc_main_{position}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SYSTEM_PROMPT


def load_variant_file(path):
    """A prompt variant file: {"description": str, "replace": [{"old": str, "new": str}, ...]},
    applied to the CURRENT prompt. Every "old" must occur exactly once, so a variant can never
    silently miss its target or edit two places."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    if not isinstance(spec.get("replace"), list) or not spec["replace"]:
        raise ValueError(f"{path}: 'replace' must be a non-empty list")
    return spec


def apply_variant(prompt, spec):
    out = prompt
    for i, r in enumerate(spec["replace"]):
        n = out.count(r["old"])
        if n != 1:
            raise ValueError(f"variant replace[{i}]: 'old' occurs {n} times in the prompt (must be exactly 1): {r['old'][:80]!r}")
        out = out.replace(r["old"], r["new"])
    return out


def variants(prompt, pid, files=None):
    deployed = prompt
    for line in PROMPT_LINES_ADDED:
        deployed = deployed.replace(line.format(pid=pid), "")
    out = {"current": prompt, "deployed": deployed}
    for name, path in (files or {}).items():
        out[name] = apply_variant(prompt, load_variant_file(path))
    return out


def scenarios(base):
    out = list(tick_scenarios(base))

    def carrier(name, team_code, idx, pos, team_id=0):
        s = copy.deepcopy(base)
        _player(s["gameState"], team_code, idx)["position"] = {"x": pos[0], "y": pos[1]}
        other = "away" if team_code == "home" else "home"
        if _player(s["gameState"], other, idx)["position"] == {"x": pos[0], "y": pos[1]}:
            _player(s["gameState"], other, idx)["position"] = {"x": -pos[0], "y": -pos[1]}
        s["gameState"]["ball"].update(position={"x": pos[0] + 0.5, "y": pos[1], "z": 0}, isFree=False,
                                      possessionAgentId=f"agentId_{idx}")
        out.append((name, team_id, s))

    carrier("fwd1_on_ball_in_range", "home", 3, (36, -4))
    carrier("mid_on_ball_midfield", "home", 2, (0, 2))
    carrier("fwd2_on_ball_wide", "home", 4, (28, 18))
    s = copy.deepcopy(base)
    s["gameState"]["ball"].update(position={"x": 5, "y": 10, "z": 0}, isFree=True, possessionAgentId=None)
    out.append(("loose_ball_midfield", 0, s))
    return out


# FWD1's layout (team 0 attacks +x, goal at (55, 0)). FWD2 gets the same situations mirrored in y,
# with the two forwards (and the two opponents with their numbers) swapping roles.
# Every entry: (team_code, index) -> (x, y). The ball sits 0.5 ahead of the carrier.
SHOOTING_LAYOUTS = {
    # 1. carrier at distOppGoal 41, no teammate clearly better placed
    "shoot_long_range": {("home", 3): (14, -5)},
    # 2. carrier at distOppGoal 22: shooting is the expected choice
    "shoot_in_range": {("home", 3): (33, -4), ("away", 4): (40, -15)},
    # 3. carrier at 41, the other forward free 13 units from goal
    "pass_better_teammate": {("home", 3): (14, -5), ("home", 4): (42, 6)},
    # 4. carrier at distOppGoal 30 with two opponents within 2.5, MID free behind as an outlet
    "under_pressure": {("home", 3): (25, -3), ("away", 1): (26.5, -3), ("away", 2): (25, -1), ("home", 2): (15, 8)},
    # 5. carrier at distOppGoal 35 with open space ahead; the other forward is marked
    "advance_open_space": {("home", 3): (20, -2), ("home", 4): (18, 15), ("away", 4): (19, 17),
                           ("away", 2): (15, 20), ("away", 3): (5, -20), ("away", 1): (8, -3)},
}


# Off-ball controls for the forwards: a teammate (MID) on the ball, and an opponent attacking our goal.
CONTROL_SCENARIOS = ("mid_on_ball_midfield", "opponent_attack")


def shooting_scenarios(base, pid):
    """The five shooting-experiment situations for FWD1 (pid 3) or, mirrored, FWD2 (pid 4)."""
    assert pid in (3, 4)
    out = []
    for name, layout in SHOOTING_LAYOUTS.items():
        s = copy.deepcopy(base)
        gs = s["gameState"]
        for (team_code, idx), (x, y) in layout.items():
            _player(gs, team_code, idx)["position"] = {"x": x, "y": y}
        if pid == 4:  # mirror y, swap #3 <-> #4 on both teams
            for team_code in ("home", "away"):
                p3, p4 = _player(gs, team_code, 3), _player(gs, team_code, 4)
                p3["position"], p4["position"] = p4["position"], p3["position"]
            for p in gs["players"]:
                p["position"] = {"x": p["position"]["x"], "y": -p["position"]["y"]}
        me = _player(gs, "home", pid)["position"]
        gs["ball"].update(position={"x": me["x"] + 0.5, "y": me["y"], "z": 0}, isFree=False,
                          possessionAgentId=f"agentId_{pid}")
        out.append((name, 0, s))
    return out


# DEF situations (2026-10-22). (name, team_id, {(team_code, index): (x, y)}, ball holder). The ball sits 0.5
# ahead of the holder. Team 0 defends x = -55; the last one plays as team 1 (defends +55) to check the sign.
DEFENDING_LAYOUTS = [
    # our ball: where does DEF stand? (the prompt asks: central, ~15 behind the most advanced teammate)
    ("our_mid_on_ball", 0, {("home", 2): (0, 2)}, ("home", 2)),
    ("our_fwd_wide", 0, {("home", 4): (28, 25)}, ("home", 4)),
    ("our_fwd_in_their_corner", 0, {("home", 4): (50, 31)}, ("home", 4)),
    ("our_fwd_deep_as_away", 1, {("away", 3): (-40, -6)}, ("away", 3)),
    # their ball: slide only close to our goal, never in their half, on a sideline or in their corner
    ("their_ball_in_their_corner", 0, {("away", 4): (50, -31)}, ("away", 4)),
    ("their_ball_wide_their_half", 0, {("away", 2): (20, 30)}, ("away", 2)),
    ("their_ball_our_sideline", 0, {("away", 4): (-35, 32)}, ("away", 4)),
    ("their_ball_near_our_goal", 0, {("away", 3): (-40, 3), ("home", 1): (-44, 1)}, ("away", 3)),
]
SLIDE_ZONE = 35.0       # the variants' "defensive third": target's distToMyGoal under this
SIDELINE_Y = 25.0       # |y| at or above this counts as "on the sideline"
WIDE_Y = 10.0           # a DEF MOVE_TO on our ball with |target_y| above this is "wide"


REAL_STATES = os.path.join(ROOT, "afc_tests", "fixtures", "real_states_20261022.json")


def real_scenarios(every=7):
    """Open-play states the match server sent in the first real match (team 0), converted to the design pitch
    exactly as the runtime does (lib/frame.py), every `every`-th one."""
    from frame import to_design_frame
    with open(REAL_STATES, encoding="utf-8") as f:
        real = [c for c in json.load(f) if c["gameState"]["playMode"] == "OPEN_PLAY"]
    out = []
    for c in real[::every]:
        gs, _ = to_design_frame(c["gameState"])
        out.append((f"real_t{c['gameState']['tick']}", c.get("teamId", 0), {**c, "gameState": gs}))
    return out


def defending_scenarios(base):
    out = []
    for name, team_id, layout, (h_team, h_idx) in DEFENDING_LAYOUTS:
        s = copy.deepcopy(base)
        gs = s["gameState"]
        for (team_code, idx), (x, y) in layout.items():
            _player(gs, team_code, idx)["position"] = {"x": x, "y": y}
        hp = _player(gs, h_team, h_idx)["position"]
        gs["ball"].update(position={"x": hp["x"] + 0.5, "y": hp["y"], "z": 0}, isFree=False,
                          possessionAgentId=f"agentId_{h_idx}")
        out.append((name, team_id, s))
    return out


def build_plan(position, models, variant_names, reps, seed=1, scenario_set="default", variant_files=None,
               enriched=None, base_prompt=None):
    """enriched: {name: base_variant}: the SAME prompt as base_variant, but the summary is built with
    summarize_state(..., teammate_detail=True). Everything else (state, validator, metrics) is shared.
    base_prompt: the text to treat as "current" instead of the position's main.py (to reproduce an
    experiment made before a prompt was changed, e.g. afc_tests/fixtures/prompts/*_pre_20261018.txt)."""
    pid, label, cfg = POSITIONS[position]
    prompts = variants(base_prompt if base_prompt is not None else load_prompt(position), pid, variant_files)
    enriched = enriched or {}
    for name, base_variant in enriched.items():
        if base_variant not in prompts:
            raise ValueError(f"enriched variant {name}: unknown base variant {base_variant}")
        prompts[name] = prompts[base_variant]
    missing = [v for v in variant_names if v not in prompts]
    if missing:
        raise ValueError(f"unknown variants {missing}; known: {sorted(prompts)}")
    with open(os.path.join(HERE, "sample_prompt.txt"), encoding="utf-8") as f:
        base = json.load(f)
    if scenario_set == "default":
        scen = scenarios(base)
    elif scenario_set == "shooting":
        scen = shooting_scenarios(base, pid)
    elif scenario_set == "real":
        scen = real_scenarios()
    elif scenario_set == "defending":
        scen = defending_scenarios(base)
    else:  # shooting_control: the five carrier situations + two off-ball controls from the default set
        by_name = {s[0]: s for s in scenarios(base)}
        scen = shooting_scenarios(base, pid) + [by_name[n] for n in CONTROL_SCENARIOS]
    combos = [(m, v) for m in models for v in variant_names]
    summaries = {n: summarize_state(d["gameState"], t, pid, label) for n, t, d in scen}
    rich = {n: summarize_state(d["gameState"], t, pid, label, teammate_detail=True) for n, t, d in scen}
    by_variant = {v: (rich if v in enriched else summaries) for v in variant_names}
    rng = random.Random(seed)
    order = []
    for rep in range(reps):
        for name, team_id, data in scen:
            batch = combos[:]
            rng.shuffle(batch)
            order += [(rep, name, team_id, data, m, v) for m, v in batch]
    est_in = statistics.mean(len(prompts[v]) + len(s) for v in variant_names for s in by_variant[v].values()) / 4
    est_usd = sum((PRICE[m][0] * est_in + PRICE[m][1] * 60) / 1e6 for _, _, _, _, m, _ in order)
    return {"position": position, "pid": pid, "label": label, "cfg": cfg, "prompts": prompts, "scenarios": scen,
            "combos": combos, "summaries": summaries, "summaryByVariant": by_variant, "enriched": enriched,
            "order": order, "calls": len(order), "estInputTokens": est_in, "estUSD": est_usd,
            "scenarioSet": scenario_set}


# --- scoring one reply --------------------------------------------------------------------------

def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def tactical_view(gs, team_id, pid, cmd):
    """Objective geometry + the HEURISTIC flags documented in the module docstring."""
    mine = "home" if team_id == 0 else "away"
    me_p = _player(gs, mine, pid)["position"]
    me = (me_p["x"], me_p["y"])
    _, opp_goal_x = get_goal_positions(team_id)
    goal = (opp_goal_x, 0.0)
    info = resolve_possession(gs["ball"], gs["players"], team_id)
    opponents = [(p["position"]["x"], p["position"]["y"]) for p in gs["players"] if p["teamCode"] != mine]
    out = {"possession": info.state.name, "holder": info.holder_idx, "myDistToGoal": round(_dist(me, goal), 1),
           "myDistOppGoalX": round(abs(me[0] - opp_goal_x), 1)}  # what the summary prints as distOppGoal
    carrier = info.is_holder(pid)
    if carrier:
        better = []
        for p in gs["players"]:
            k = int(str(p["agentId"]).rsplit("_", 1)[-1])
            if p["teamCode"] == mine and k != pid:
                tp = (p["position"]["x"], p["position"]["y"])
                if _dist(tp, goal) <= out["myDistToGoal"] - BETTER_MARGIN and all(_dist(tp, o) > 5 for o in opponents):
                    better.append(k)
        out.update(carrier=True, betterTeammates=better,
                   pressure=sum(1 for o in opponents if _dist(me, o) <= PRESSURE_RADIUS))
    if cmd is None:
        return out
    ct, par = cmd.get("commandType"), cmd.get("parameters") or {}
    if ct == "SHOOT":
        out["shotDistance"] = out["myDistToGoal"]
        out["shotDistX"] = out["myDistOppGoalX"]
        out["shot_over_threshold"] = out["myDistOppGoalX"] > SHOT_THRESHOLD
        out["long_shot"] = out["myDistToGoal"] > LONG_SHOT
        out["better_teammate"] = bool(out.get("betterTeammates"))
    if ct == "PASS" and carrier:
        tgt = par.get("target_player_id")
        out["passTarget"] = tgt
        out["pass_to_better"] = tgt in out["betterTeammates"]
        tp = next((p["position"] for p in gs["players"] if p["teamCode"] == mine
                   and int(str(p["agentId"]).rsplit("_", 1)[-1]) == tgt), None)
        if tp:
            toward = 1 if opp_goal_x > 0 else -1
            out["passDx"] = round((tp["x"] - me[0]) * toward, 1)       # + forward, - backward
            out["backward_pass"] = out["passDx"] < -2
            out["pass_to_backline"] = tgt in (0, 1)                    # GK or DEF
            out["passTargetNearestOpp"] = round(min(_dist((tp["x"], tp["y"]), o) for o in opponents), 1)
        out["under_pressure"] = out["pressure"] >= 2
    if ct == "MOVE_TO" and carrier:
        tgt = (par.get("target_x", me[0]), par.get("target_y", me[1]))
        gain = _dist(me, goal) - _dist(tgt, goal)
        out["carrierGain"] = round(gain, 1)
        out["advance"] = gain >= 5
        # heuristic "reasonable": the advance does not end on top of an opponent
        out["advance_clear"] = gain >= 5 and min(_dist(tgt, o) for o in opponents) >= 3
        out["stand_still"] = _dist(me, tgt) < 2
    if ct == "MOVE_TO" and info.state is Possession.OURS and info.holder_idx != pid:
        gain = _dist(me, goal) - _dist((par.get("target_x", me[0]), par.get("target_y", me[1])), goal)
        out["forwardGain"] = round(gain, 1)
        out["support_run"] = gain >= 5
    if ct in DEFENSIVE and info.state is Possession.OURS:
        out["defend_own_ball"] = True
    _defender_view(out, gs, mine, team_id, pid, info, me, ct, par)
    return out


def _defender_view(out, gs, mine, team_id, pid, info, me, ct, par):
    """HEURISTIC flags for the DEF shape rules: where slides and chases happen, where DEF stands on our ball."""
    my_goal_x, opp_goal_x = get_goal_positions(team_id)
    toward = 1 if opp_goal_x > 0 else -1

    def opponent(k):
        return next((p["position"] for p in gs["players"] if p["teamCode"] != mine
                     and int(str(p["agentId"]).rsplit("_", 1)[-1]) == k), None)

    if ct in ("SLIDE_TACKLE", "MARK", "FOLLOW_PLAYER"):
        k = par.get("target_player_id")
        if ct == "SLIDE_TACKLE" and k in (None, -1) and info.state is Possession.THEIRS:
            k = info.holder_idx
        tp = opponent(k) if isinstance(k, int) else None
        if tp:
            d = abs(tp["x"] - my_goal_x)
            out["targetDistToMyGoal"] = round(d, 1)
            if ct == "SLIDE_TACKLE":
                out["slide_outside_zone"] = d >= SLIDE_ZONE or abs(tp["y"]) >= SIDELINE_Y
            else:
                out["chase_opp_half"] = d > 55
    if ct == "MOVE_TO" and info.state is Possession.OURS and info.holder_idx != pid:
        tx, ty = par.get("target_x", me[0]), par.get("target_y", me[1])
        mates = [p["position"]["x"] for p in gs["players"] if p["teamCode"] == mine
                 and int(str(p["agentId"]).rsplit("_", 1)[-1]) != pid]
        adv = max(mates, key=lambda x: x * toward)
        out["shapeBehind"] = round((adv - tx) * toward, 1)   # + = closer to our goal than the most advanced mate
        out["shapeY"] = ty
        out["shape_wide"] = abs(ty) > WIDE_Y
        out["shape_past_halfway"] = tx * toward > 0
        out["shape_ahead_of_attack"] = out["shapeBehind"] < 0


def score_reply(text, gs, team_id, pid, cfg):
    raw = extract_reply(text)
    parsed = isinstance(raw, (list, dict))
    v = validate_reply(raw, context_from_state(gs, team_id, pid))
    codes = [i.code for i in v.issues]
    cmd = v.commands[0] if v.ok else None
    row = {"parsed": parsed, "accepted": v.ok, "reason": v.reason, "issues": [str(i) for i in v.issues],
           "codes": codes, "protocolError": any(c not in POSSESSION_CODES for c in codes),  # format/ids/params, not football
           "possessionRejected": any(c in POSSESSION_CODES for c in codes),
           "command": cmd["commandType"] if cmd else None, "parameters": cmd.get("parameters") if cmd else None}
    if not v.ok:  # what the runtime would have sent instead
        fb = build_fallback(cfg)(gs, team_id, pid)
        row["fallbackCommand"] = fb[0]["commandType"]
        row["fallbackParameters"] = fb[0].get("parameters")
    raw_cmd = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], dict) else (raw if isinstance(raw, dict) else None)
    row["rawCommandType"] = raw_cmd.get("commandType") if raw_cmd else None
    row["tactical"] = tactical_view(gs, team_id, pid, cmd)
    return row


# --- running ------------------------------------------------------------------------------------

class Budget:
    def __init__(self, max_calls, max_usd, max_consecutive_errors=3):
        self.max_calls, self.max_usd, self.max_err = max_calls, max_usd, max_consecutive_errors
        self.http_requests, self.usd, self.consecutive_errors = 0, 0.0, 0

    def stop_reason(self):
        if self.http_requests > self.max_calls:
            return f"HTTP requests {self.http_requests} > planned {self.max_calls}"
        if self.usd > self.max_usd:
            return f"estimated cost ${self.usd:.4f} > --max-usd {self.max_usd}"
        if self.consecutive_errors >= self.max_err:
            return f"{self.consecutive_errors} failed calls in a row"
        return None


def run(plan, call_fn, budget, sink=None):
    """call_fn(model, prompt, summary) -> {text, wallS, bedrockMs, inTok, outTok} or raises."""
    rows = []
    for rep, name, team_id, data, m, v in plan["order"]:
        reason = budget.stop_reason()
        if reason:
            return rows, reason
        row = {"model": m, "variant": v, "scenario": name, "team": team_id, "rep": rep}
        try:
            res = call_fn(m, plan["prompts"][v], plan["summaryByVariant"][v][name])
        except Exception as e:  # recorded, never retried (a retry is another paid call)
            budget.consecutive_errors += 1
            row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        else:
            budget.consecutive_errors = 0
            # Some streamed replies arrive without usage metadata (Strands reports 0 tokens): count
            # them at the plan's estimate (and 60 output tokens) so cost and the budget stay conservative.
            row["usageMissing"] = not res.get("inTok") or not res.get("outTok")
            tin = res.get("inTok") or plan["estInputTokens"]
            tout = res.get("outTok") or 60
            usd = (PRICE[m][0] * tin + PRICE[m][1] * tout) / 1e6
            budget.usd += usd
            row.update(res, usd=usd, cumulativeUSD=budget.usd)
            row.update(score_reply(res["text"], data["gameState"], team_id, plan["pid"], plan["cfg"]))
        row["httpRequestsSoFar"] = budget.http_requests
        rows.append(row)
        if sink:
            sink.write(json.dumps(row, default=str) + "\n")
            sink.flush()
    return rows, budget.stop_reason()


def make_bedrock_caller(region, profile, models, budget):
    import boto3
    from botocore.config import Config
    from strands import Agent, ModelRetryStrategy
    from strands.agent.conversation_manager import SlidingWindowConversationManager
    from strands.models import BedrockModel

    session = boto3.Session(profile_name=profile, region_name=region)
    cfg = Config(retries={"total_max_attempts": 1, "mode": "standard"}, read_timeout=60, connect_timeout=10)
    shared = {}
    for m in models:
        extra = {"additional_request_fields": MODEL_FIELDS[m]} if m in MODEL_FIELDS else {}
        bm = BedrockModel(model_id=MODELS[m], boto_session=session, boto_client_config=cfg, **extra)

        def count(**kw):
            budget.http_requests += 1
        bm.client.meta.events.register("before-send.bedrock-runtime", count)
        shared[m] = bm

    def call(m, prompt, summary):
        agent = Agent(model=shared[m], system_prompt=prompt, callback_handler=None,
                      retry_strategy=ModelRetryStrategy(max_attempts=1),
                      conversation_manager=SlidingWindowConversationManager(window_size=0))
        t0 = time.perf_counter()
        res = agent(summary)
        wall = time.perf_counter() - t0
        usage, metrics = res.metrics.accumulated_usage, res.metrics.accumulated_metrics
        return {"text": str(res), "wallS": round(wall, 3), "bedrockMs": metrics.get("latencyMs"),
                "inTok": usage.get("inputTokens"), "outTok": usage.get("outputTokens")}
    return call


# --- reporting ----------------------------------------------------------------------------------

def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summarize(rows, combos):
    out = {}
    for m, v in combos:
        rs = [r for r in rows if r["model"] == m and r["variant"] == v]
        ok = [r for r in rs if "error" not in r]
        acc = [r for r in ok if r["accepted"]]
        n = len(ok) or 1
        tac = [r["tactical"] for r in ok if r.get("tactical")]
        support_cases = [t for t in tac if "support_run" in t]
        out[f"{m}/{v}"] = {
            "calls": len(rs), "apiErrors": len(rs) - len(ok),
            "parsedPct": 100 * sum(r["parsed"] for r in ok) / n, "acceptedPct": 100 * len(acc) / n,
            "fallbackPct": 100 * (len(ok) - len(acc)) / n,
            "protocolErrors": sum(r["protocolError"] for r in ok),
            "possessionRejections": sum(r["possessionRejected"] for r in ok),
            "rejectionCodes": dict(Counter(c for r in ok for c in r["codes"])),
            "longShots": sum(1 for t in tac if t.get("long_shot")),
            "shots": sum(1 for t in tac if "shotDistance" in t),
            "betterTeammateShots": sum(1 for t in tac if t.get("better_teammate")),
            "supportRuns": f"{sum(1 for t in support_cases if t['support_run'])}/{len(support_cases)}",
            "defendOwnBall": sum(1 for t in tac if t.get("defend_own_ball")),
            "wallP50": percentile([r["wallS"] for r in ok], .5), "wallP95": percentile([r["wallS"] for r in ok], .95),
            "bedrockMsP50": percentile([r["bedrockMs"] for r in ok if r.get("bedrockMs")], .5),
            "inTokMean": statistics.mean([r["inTok"] for r in ok if r.get("inTok")] or [0]),
            "outTokMean": statistics.mean([r["outTok"] for r in ok if r.get("outTok")] or [0]),
            "usd": sum(r.get("usd", 0) for r in rs),
            "usageMissing": sum(1 for r in ok if r.get("usageMissing")),
            "commands": dict(Counter(r["command"] or f"(rejected:{r['rawCommandType']})" for r in ok)),
        }
        # carrier decisions (shooting experiment), counted over accepted replies where I hold the ball
        car = [r for r in ok if r.get("tactical", {}).get("carrier")]
        car_acc = [r for r in car if r["accepted"]]
        shots = [r["tactical"] for r in car_acc if r["command"] == "SHOOT"]
        far = [r for r in car if r["tactical"]["myDistOppGoalX"] > SHOT_THRESHOLD]
        far_acc = [r for r in far if r["accepted"]]
        near = [r for r in car if r["tactical"]["myDistOppGoalX"] <= SHOT_THRESHOLD]
        near_acc = [r for r in near if r["accepted"]]
        better = [r for r in car if r["tactical"].get("betterTeammates")]
        out[f"{m}/{v}"].update({
            "carrierReplies": len(car),
            "shotsOverThreshold": sum(1 for t in shots if t["shot_over_threshold"]),
            "shotsWithinThreshold": sum(1 for t in shots if not t["shot_over_threshold"]),
            "farCarrierReplies": len(far), "farAccepted": len(far_acc),
            "farShootPct": 100 * sum(1 for r in far_acc if r["command"] == "SHOOT") / (len(far_acc) or 1),
            "nearCarrierReplies": len(near), "nearAccepted": len(near_acc),
            "nearShootPct": 100 * sum(1 for r in near_acc if r["command"] == "SHOOT") / (len(near_acc) or 1),
            "betterTeammateCases": len(better),
            "passToBetter": sum(1 for r in better if r["accepted"] and r["tactical"].get("pass_to_better")),
            "carrierPasses": sum(1 for r in car_acc if r["command"] == "PASS"),
            "passTargets": dict(Counter(str(r["tactical"].get("passTarget")) for r in car_acc if r["command"] == "PASS")),
            "underPressureChoices": dict(Counter(
                (r["command"] + (f">{r['tactical'].get('passTarget')}" if r["command"] == "PASS" else "")) if r["accepted"]
                else f"(rejected:{r['rawCommandType']})" for r in car if r["tactical"].get("pressure", 0) >= 2)),
            "carrierAdvances": sum(1 for r in car_acc if r["tactical"].get("advance")),
            "carrierAdvancesClear": sum(1 for r in car_acc if r["tactical"].get("advance_clear")),
            "carrierStandStill": sum(1 for r in car_acc if r["tactical"].get("stand_still")),
            "backwardPasses": sum(1 for r in car_acc if r["tactical"].get("backward_pass")),
            "passesToBackline": sum(1 for r in car_acc if r["tactical"].get("pass_to_backline")),
            "pressureCases": sum(1 for r in car if r["tactical"].get("pressure", 0) >= 2),
            "backwardPassesUnderPressure": sum(1 for r in car_acc if r["tactical"].get("under_pressure")
                                               and r["tactical"].get("backward_pass")),
            "forwardOrLevelPassesUnderPressure": sum(1 for r in car_acc if r["tactical"].get("under_pressure")
                                                     and r["command"] == "PASS" and not r["tactical"].get("backward_pass")),
        })
        # DEF shape (defending set): slides, chases, and the spot taken on our ball
        shape = [t for t in tac if "shapeBehind" in t]
        out[f"{m}/{v}"].update({
            "slides": sum(1 for r in ok if r["command"] == "SLIDE_TACKLE"),
            "slidesOutsideZone": sum(1 for t in tac if t.get("slide_outside_zone")),
            "chasesInOppHalf": sum(1 for t in tac if t.get("chase_opp_half")),
            "ourBallMoves": len(shape),
            "ourBallMovesWide": sum(1 for t in shape if t["shape_wide"]),
            "ourBallMovesPastHalfway": sum(1 for t in shape if t["shape_past_halfway"]),
            "ourBallMovesAheadOfAttack": sum(1 for t in shape if t["shape_ahead_of_attack"]),
            "ourBallBehindMedian": statistics.median([t["shapeBehind"] for t in shape]) if shape else None,
        })
        # off-ball (control scenarios): what I do when I do not hold the ball
        off = [r for r in ok if not r.get("tactical", {}).get("carrier")]
        out[f"{m}/{v}"].update({
            "offBallReplies": len(off),
            "offBallCommands": dict(Counter(r["command"] or f"(rejected:{r['rawCommandType']})" for r in off)),
        })
    return out


def scenario_table(rows, combos, scenario_names):
    """Per scenario and config: the commands chosen (rejected ones as X:<raw type>), in rep order."""
    def short(r):
        if "error" in r:
            return "ERR"
        if not r["accepted"]:
            return f"X:{r['rawCommandType']}"
        c, p = r["command"], r["parameters"] or {}
        if c == "MOVE_TO":
            return f"MOVE({p.get('target_x'):g},{p.get('target_y'):g}{',S' if p.get('sprint') else ''})"
        if c == "PASS":
            return f"PASS>{p.get('target_player_id')}"
        if c == "SHOOT":
            return "SHOOT"
        return c
    lines = []
    for s in scenario_names:
        lines.append(f"== {s}")
        for m, v in combos:
            rs = sorted((r for r in rows if r["scenario"] == s and r["model"] == m and r["variant"] == v), key=lambda r: r["rep"])
            lines.append(f"   {m + '/' + v:22s} " + " | ".join(short(r) for r in rs))
    return lines


def unique_path(path):
    base, ext = os.path.splitext(path)
    k = 1
    while os.path.exists(path):
        path = f"{base}-{k}{ext}"
        k += 1
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--position", choices=POSITIONS, default="fwd1")
    ap.add_argument("--models", default="micro,lite")
    ap.add_argument("--variants", default="current,deployed")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION") or "us-east-1")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--max-usd", type=float, default=0.05, help="stop when the estimated cost exceeds this")
    ap.add_argument("--max-calls", type=int, help="refuse to start when the plan has more calls than this")
    ap.add_argument("--scenario-set", choices=("default", "shooting", "shooting_control", "defending", "real"), default="default")
    ap.add_argument("--base-prompt", metavar="PATH", help="use this text as the 'current' prompt (reproduce older experiments)")
    ap.add_argument("--enriched", action="append", default=[], metavar="NAME=BASE_VARIANT",
                    help="same prompt as BASE_VARIANT, summary with teammate_detail=True (distOppGoal, nearestOpp)")
    ap.add_argument("--variant-file", action="append", default=[], metavar="NAME=PATH",
                    help="extra prompt variant (JSON replace spec, applied to the current prompt)")
    ap.add_argument("--invoke", action="store_true", help="PAID: call Bedrock")
    args = ap.parse_args()

    models, names = args.models.split(","), args.variants.split(",")
    files = dict(item.split("=", 1) for item in args.variant_file)
    enriched = dict(item.split("=", 1) for item in args.enriched)
    base_prompt = open(args.base_prompt, encoding="utf-8", newline="").read() if args.base_prompt else None
    plan = build_plan(args.position, models, names, args.reps, args.seed, args.scenario_set, files, enriched, base_prompt)
    if args.max_calls is not None and plan["calls"] > args.max_calls:
        print(f"REFUSED: the plan has {plan['calls']} calls, more than --max-calls {args.max_calls}")
        return 3
    print(f"{plan['label']} (player {plan['pid']}) | {len(plan['scenarios'])} scenarios x {args.reps} reps x "
          f"{len(plan['combos'])} combos = {plan['calls']} calls")
    for v in names:
        print(f"   variant {v:8s}: {len(plan['prompts'][v])} prompt chars")
    for m in models:
        print(f"   model {m:9s}: {MODELS[m]} {MODEL_FIELDS.get(m, '')}  ${PRICE[m][0]}/{PRICE[m][1]} per 1M in/out")
    print(f"   estimate: ~{plan['estInputTokens']:.0f} input tokens/call (chars/4), <=60 output -> about "
          f"${plan['estUSD']:.4f}; stop at ${args.max_usd} or {plan['calls']} HTTP requests")
    if not args.invoke:
        print("Plan only. Re-run with --invoke (paid).")
        return 0

    budget = Budget(plan["calls"], args.max_usd)
    call = make_bedrock_caller(args.region, args.profile, models, budget)
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    stem = unique_path(os.path.join(HERE, "reports", f"model-eval-{args.position}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
    with open(stem, "w", encoding="utf-8") as sink:
        rows, stopped = run(plan, call, budget, sink)
    summary = summarize(rows, plan["combos"])
    out = unique_path(stem[:-1])
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"position": args.position, "region": args.region, "seed": args.seed, "reps": args.reps,
                   "scenarioSet": args.scenario_set, "variantFiles": files, "basePrompt": args.base_prompt,
                   "plannedCalls": plan["calls"], "callsMade": len(rows), "httpRequests": budget.http_requests,
                   "estimatedUSD": budget.usd, "stopped": stopped, "summary": summary,
                   "prompts": plan["prompts"], "enriched": plan["enriched"],
                   "summaryByVariant": plan["summaryByVariant"]}, f, indent=1)
    print(f"\ncalls {len(rows)}/{plan['calls']} | HTTP requests {budget.http_requests} | estimated ${budget.usd:.5f}"
          + (f" | STOPPED: {stopped}" if stopped and len(rows) < plan["calls"] else ""))
    hdr = f"{'config':15s} {'parsed':>7s} {'accept':>7s} {'proto':>5s} {'poss':>5s} {'p50':>5s} {'p95':>5s} {'in':>4s} {'out':>4s} {'USD':>8s} {'long/shots':>10s} {'support':>7s}"
    print(hdr)
    for k, s in summary.items():
        print(f"{k:15s} {s['parsedPct']:6.0f}% {s['acceptedPct']:6.0f}% {s['protocolErrors']:5d} {s['possessionRejections']:5d} "
              f"{s['wallP50'] or 0:5.2f} {s['wallP95'] or 0:5.2f} {s['inTokMean']:4.0f} {s['outTokMean']:4.0f} {s['usd']:8.5f} "
              f"{s['longShots']:>4d}/{s['shots']:<5d} {s['supportRuns']:>7s}" + (f"  errors {s['apiErrors']}" if s["apiErrors"] else ""))
    if args.scenario_set in ("shooting", "shooting_control"):
        print(f"\ncarrier decisions (threshold {SHOT_THRESHOLD:g} on distOppGoal):")
        for k, s in summary.items():
            print(f"   {k:22s} shots >{SHOT_THRESHOLD:g}: {s['shotsOverThreshold']:2d} | far: shoot {s['farShootPct']:3.0f}% of {s['farAccepted']} accepted"
                  f" | near: shoot {s['nearShootPct']:3.0f}% of {s['nearAccepted']} | pass to better teammate {s['passToBetter']}/{s['betterTeammateCases']}"
                  f" | passes {s['carrierPasses']} (backward {s['backwardPasses']}, to GK/DEF {s['passesToBackline']})"
                  f" | under pressure: backward {s['backwardPassesUnderPressure']}, forward/level {s['forwardOrLevelPassesUnderPressure']} of {s['pressureCases']}"
                  f" | advances {s['carrierAdvances']} (clear {s['carrierAdvancesClear']}) | stand still {s['carrierStandStill']}"
                  f"\n   {'':22s} pass targets {s['passTargets']} | under pressure {s['underPressureChoices']}")
        print("\noff-ball commands:")
        for k, s in summary.items():
            print(f"   {k:22s} {s['offBallCommands']}")
    if args.scenario_set == "defending":
        print(f"\nDEF shape (slide zone: target distToMyGoal < {SLIDE_ZONE:g} and |y| < {SIDELINE_Y:g}):")
        for k, s in summary.items():
            print(f"   {k:22s} slides {s['slides']} (outside zone {s['slidesOutsideZone']}) | chases into their half {s['chasesInOppHalf']}"
                  f" | our-ball MOVE_TO {s['ourBallMoves']}: wide {s['ourBallMovesWide']}, past halfway {s['ourBallMovesPastHalfway']},"
                  f" ahead of attack {s['ourBallMovesAheadOfAttack']}, behind median {s['ourBallBehindMedian']}")
    print()
    for line in scenario_table(rows, plan["combos"], [n for n, _, _ in plan["scenarios"]]):
        print(line)
    print(f"\nrows: {os.path.relpath(stem, ROOT)}\nsummary: {os.path.relpath(out, ROOT)}")
    return 0 if not stopped or len(rows) == plan["calls"] else 2


if __name__ == "__main__":
    sys.exit(main())
