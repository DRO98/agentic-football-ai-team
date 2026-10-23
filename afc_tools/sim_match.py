"""SIMULATED multi-tick matches for our five agents. Offline by default: no AWS, no model, no cost.

    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\sim_match.py                       # 20 matches x 200 ticks
    C:\\dev\\afc\\.venv\\Scripts\\python.exe afc_tools\\sim_match.py --matches 50 --seed 7

WHAT IS REAL and what is invented here:
  REAL (the code that is deployed): every tick, each of our five players goes through the real
      handler from lib/agent_base.py with its real fallback config and the GK's real rule
      (lib/rules.py): summary -> rule -> validator -> fallback -> last resort. The game state has
      the same shape as afc_tools/sample_prompt.txt, including both teams using agentId_0..4.
  SIMULATED (my own toy physics, NOT the match server's rules): speeds, ball pick-up, tackles,
      pass and shot outcomes, the opponent's scripted play, kick-off after a goal. Scores and
      possession % are therefore indicative only. What IS meaningful: whether our pipeline stays
      valid over evolving states, how often our possession inference disagrees with the truth,
      and how often each rule/fallback path fires.

The "model" here is a stand-in that never answers (so every tick exercises rule + validator +
fallback): the team plays on its deterministic layers alone. This is the floor the LLM sits on.

Metrics (per player and team), all counted from the simulation:
  invalid         handler outputs the validator rejects (must be 0)
  incompatible    on-ball command (PASS/SHOOT/GK_DISTRIBUTE) while not the true holder, or PRESS_BALL/
                  INTERCEPT while our team truly holds the ball (must be 0)
  inference       our resolve_possession() vs the simulator's truth (OURS/THEIRS/LOOSE), e.g.
                  truth OURS seen as UNKNOWN: the case where the same-number opponent is near
  sources         rule / fallback / last_resort
  situations      how often candidate rule situations occur (input for deciding rules)
"""

import argparse
import asyncio
import copy
import json
import math
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
from test_helpers import mock_agentcore  # noqa: E402

mock_agentcore()
import agent_base  # noqa: E402
from action_validator import context_from_state, validate_reply  # noqa: E402
from fallback import DEF_CONFIG, FWD1_CONFIG, FWD2_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback  # noqa: E402
from possession import Possession, resolve_possession  # noqa: E402
from rules import ball_in_own_penalty_area, carrier_shoot, chain, def_shape_on_our_ball, fwd1_support, fwd2_poacher, gk_distribute, gk_loose_ball_in_box, own_half_release  # noqa: E402

# --- SIMULATED physics constants (invented) ---------------------------------------------------
DT = 0.5                    # game seconds per tick
WALK, SPRINT = 4.0, 7.0     # units per second
PICKUP = 1.0                # a loose ball within this distance is picked up by the nearest player
STEAL_P = 0.25              # per tick, PRESS_BALL/INTERCEPT within 1.5 of the holder
SLIDE_P = 0.5
PASS_CUT = 3.0              # an opponent within this of the pass midpoint cuts it
DRIBBLE_OFFSET = 0.6        # the ball sits this far ahead of its holder
FIELD_X, FIELD_Y = 55.0, 35.0
HOME_SHAPE = [(-50, 0), (-30, 0), (-10, -8), (10, -6), (12, 10)]
LABELS = ["GK", "DEF", "MID", "FWD1", "FWD2"]
CONFIGS = [GK_CONFIG, DEF_CONFIG, MID_CONFIG, FWD1_CONFIG, FWD2_CONFIG]
ON_BALL = {"PASS", "SHOOT", "GK_DISTRIBUTE"}
TO_BALL = {"PRESS_BALL", "INTERCEPT"}


class _App:
    def __init__(self):
        self.records = []
        rec = self.records

        class Log:
            def info(self, m): rec.append(m)
            def warn(self, m): rec.append(m)
            def error(self, m): rec.append(m)
        self.logger = Log()

    def entrypoint(self, fn):
        return fn


class _SilentModel:
    """Stand-in for the LLM that never gives a usable answer: every tick runs on rule/fallback."""
    def __call__(self, prompt):
        return "(simulated: no model)"


def build_team():
    handlers = []
    for pid, (label, cfg) in enumerate(zip(LABELS, CONFIGS)):
        app = _App()
        rule = {0: chain(gk_loose_ball_in_box, gk_distribute), 1: chain(own_half_release, carrier_shoot, def_shape_on_our_ball), 2: chain(own_half_release, carrier_shoot),  # as each main.py wires it
                3: chain(own_half_release, carrier_shoot, fwd1_support), 4: chain(own_half_release, carrier_shoot, fwd2_poacher)}[pid]
        h = agent_base.create_invoke_handler(app, _SilentModel(), pid, label, build_fallback(cfg), cfg, rule_fn=rule)
        handlers.append((h, app))
    return handlers


def run_handler(loop, handler, app, state, team_id, pid):
    async def collect():
        payload = {"prompt": json.dumps({"gameState": state, "teamId": team_id, "myPlayers": [pid]})}
        return [c async for c in handler(payload, None)]
    start = len(app.records)
    out = json.loads(loop.run_until_complete(collect())[0])
    lines = app.records[start:]
    source = ("rule" if any("answered by rule" in m for m in lines) else
              "last_resort" if any("using last resort" in m for m in lines) else
              "error" if any("agent error" in m for m in lines) else "fallback")
    return out, source


# --- the simulated world -----------------------------------------------------------------------

def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clamp(p):
    return (max(-FIELD_X, min(FIELD_X, p[0])), max(-FIELD_Y, min(FIELD_Y, p[1])))


def step_toward(p, target, speed):
    d = dist(p, target)
    if d < 1e-9:
        return p
    k = min(1.0, speed * DT / d)
    return clamp((p[0] + (target[0] - p[0]) * k, p[1] + (target[1] - p[1]) * k))


class World:
    def __init__(self, rng):
        self.rng = rng
        self.tick, self.score = 0, [0, 0]
        self.kickoff(first=True)

    def kickoff(self, first=False):
        self.pos = {(0, i): p for i, p in enumerate(HOME_SHAPE)}
        self.pos.update({(1, i): (-x, -y) for i, (x, y) in enumerate(HOME_SHAPE)})
        if first:
            self.stamina = {k: 1.0 for k in self.pos}
        self.sprinting = {k: False for k in self.pos}
        self.holder = None
        self.ball = (self.rng.uniform(-2, 2), self.rng.uniform(-2, 2))

    def game_state(self):
        players = []
        for (team, i), (x, y) in sorted(self.pos.items()):
            players.append({"agentId": f"agentId_{i}", "teamCode": "home" if team == 0 else "away",
                            "position": {"x": round(x, 2), "y": round(y, 2)}, "velocity": {"x": 0, "y": 0},
                            "orientation": 0, "stamina": round(self.stamina[(team, i)], 3), "currentAction": 0,
                            "lastAction": "", "speed": 0, "isSprinting": self.sprinting[(team, i)]})
        return {"tick": self.tick, "gameTime": round(self.tick * DT, 1), "playMode": "OPEN_PLAY", "modeTeamId": None,
                "score": {"home": self.score[0], "away": self.score[1]},
                "ball": {"position": {"x": round(self.ball[0], 2), "y": round(self.ball[1], 2), "z": 0},
                         "velocity": {"x": 0, "y": 0, "z": 0}, "isFree": self.holder is None,
                         "possessionAgentId": f"agentId_{self.holder[1]}" if self.holder else None,
                         "rotation": {}, "angularVelocity": {}},
                "players": players, "teamChat": []}

    def truth(self, team):
        if self.holder is None:
            return Possession.LOOSE
        return Possession.OURS if self.holder[0] == team else Possession.THEIRS

    def goal_x(self, team):  # the goal a team defends
        return -FIELD_X if team == 0 else FIELD_X

    # --- applying commands (SIMULATED semantics) ---
    def apply(self, team, i, cmd):
        me, p = (team, i), self.pos[(team, i)]
        ct, par = cmd["commandType"], cmd.get("parameters", {})
        sprint = bool(par.get("sprint")) or ct in TO_BALL
        speed = (SPRINT if sprint else WALK) * (0.6 + 0.4 * self.stamina[me])
        self.sprinting[me] = sprint
        self.stamina[me] = max(0.0, min(1.0, self.stamina[me] + (-0.01 if sprint else 0.003)))
        if ct == "MOVE_TO":
            self.pos[me] = step_toward(p, (par["target_x"], par["target_y"]), speed)
        elif ct in TO_BALL:
            self.pos[me] = step_toward(p, self.ball, speed)
            if self.holder and self.holder[0] != team and dist(self.pos[me], self.pos[self.holder]) < 1.5:
                if self.rng.random() < STEAL_P:
                    self.holder = me
        elif ct == "SLIDE_TACKLE":
            if self.holder and self.holder[0] != team and dist(p, self.pos[self.holder]) < par.get("distance", 2) + 1:
                if self.rng.random() < SLIDE_P:
                    self.holder = None
        elif ct == "MARK":
            tgt = self.pos.get((1 - team, par["target_player_id"]))
            if tgt:
                gx = self.goal_x(team)
                self.pos[me] = step_toward(p, (tgt[0] + (2 if gx > tgt[0] else -2), tgt[1]), speed)
        elif ct == "FOLLOW_PLAYER":
            tt = 0 if par["target_team"] == "HOME" else 1
            tgt = self.pos.get((tt, par["target_player_id"]))
            if tgt and dist(p, tgt) > par.get("distance", 2):
                self.pos[me] = step_toward(p, tgt, speed)
        elif ct in ("PASS", "GK_DISTRIBUTE") and self.holder == me:
            tgt = self.pos.get((team, par["target_player_id"]))
            if tgt:
                mid = ((p[0] + tgt[0]) / 2, (p[1] + tgt[1]) / 2)
                cut = [k for k in self.pos if k[0] != team and dist(self.pos[k], mid) < PASS_CUT]
                if cut:
                    self.holder, self.ball = None, mid
                else:
                    self.holder = (team, par["target_player_id"])
        elif ct == "SHOOT" and self.holder == me:
            gx = self.goal_x(1 - team)
            d = dist(p, (gx, 0))
            if d <= 30 and self.rng.random() < max(0.05, 0.55 * (1 - d / 30)):
                self.score[team] += 1
                self.kickoff()
                return "goal"
            self.holder, self.ball = None, (gx - math.copysign(3, gx), self.rng.uniform(-8, 8))
        return None

    def opponent_command(self, team, i):
        """Scripted opponent (invented): holder dribbles at goal and shoots inside 18, the nearest
        player chases, the GK holds its line, the rest keep shape."""
        me, p = (team, i), self.pos[(team, i)]
        gx_att = self.goal_x(1 - team)
        if self.holder == me:
            if dist(p, (gx_att, 0)) < 18:
                return {"commandType": "SHOOT", "parameters": {"aim_location": "CENTER", "power": 0.8}}
            return {"commandType": "MOVE_TO", "parameters": {"target_x": gx_att, "target_y": 0, "sprint": True}}
        if i == 0:
            return {"commandType": "MOVE_TO", "parameters": {"target_x": self.goal_x(team) * 0.92,
                                                             "target_y": max(-6, min(6, self.ball[1]))}}
        mates = [k for k in self.pos if k[0] == team and k[1] != 0]
        if min(mates, key=lambda k: dist(self.pos[k], self.ball)) == me and (self.holder is None or self.holder[0] != team):
            return {"commandType": "PRESS_BALL", "parameters": {"intensity": 0.8}}
        sx, sy = HOME_SHAPE[i]
        return {"commandType": "MOVE_TO", "parameters": {"target_x": -sx if team == 1 else sx, "target_y": -sy if team == 1 else sy}}

    def settle_ball(self):
        if self.holder:
            hx, hy = self.pos[self.holder]
            gx = self.goal_x(1 - self.holder[0])
            self.ball = (hx + math.copysign(DRIBBLE_OFFSET, gx), hy)
            return
        near = sorted(self.pos, key=lambda k: dist(self.pos[k], self.ball))
        if dist(self.pos[near[0]], self.ball) <= PICKUP:
            self.holder = near[0]
            self.settle_ball()


def situations(world, team, gs):
    """Candidate rule situations, counted per tick from OUR team's point of view (truth-based)."""
    out = []
    h = world.holder
    if h == (team, 0):
        out.append("gk_holds_ball")
    if h is None and ball_in_own_penalty_area(gs["ball"]["position"], team):
        out.append("loose_ball_in_our_box")
    if h and h[0] == team and h[1] != 0:
        d = dist(world.pos[h], (world.goal_x(1 - team), 0))
        out.append("our_outfield_holder_in_range" if d <= 25 else "our_outfield_holder_far")
    if h and h[0] != team and dist(world.pos[h], (world.goal_x(team), 0)) <= 25:
        out.append("opponent_on_ball_near_our_goal")
    return out


def play(seed, ticks, team_handlers, loop, stats):
    rng = random.Random(seed)
    w = World(rng)
    our = 0
    for _ in range(ticks):
        gs = w.game_state()
        truth = w.truth(our)
        seen = resolve_possession(gs["ball"], gs["players"], our)
        stats["inference"][f"{truth.name}->{seen.state.name}"] += 1
        for s in situations(w, our, gs):
            stats["situations"][s] += 1
        cmds = {}
        for pid, (h, app) in enumerate(team_handlers):
            out, source = run_handler(loop, h, app, copy.deepcopy(gs), our, pid)
            stats["sources"][f"{LABELS[pid]}:{source}"] += 1
            v = validate_reply(out, context_from_state(gs, our, pid))
            if not v.ok:
                stats["invalid"].append((seed, w.tick, pid, [str(i) for i in v.issues]))
            cmd = out[0]
            stats["commands"][f"{LABELS[pid]}:{cmd['commandType']}"] += 1
            if (cmd["commandType"] in ON_BALL and w.holder != (our, pid)) or \
               (cmd["commandType"] in TO_BALL and truth is Possession.OURS):
                stats["incompatible"].append((seed, w.tick, pid, cmd["commandType"], truth.name))
            cmds[pid] = cmd
        chasers = sum(1 for c in cmds.values() if c["commandType"] in TO_BALL)
        stats["chasers"][chasers] += 1
        # apply: ours, then theirs, in a random order so neither side always moves first
        order = [(our, i) for i in range(5)] + [(1 - our, i) for i in range(5)]
        rng.shuffle(order)
        for team, i in order:
            cmd = cmds[i] if team == our else w.opponent_command(team, i)
            if w.apply(team, i, cmd) == "goal":
                break
            w.settle_ball()
        stats["possession_truth"][w.truth(our).name] += 1
        w.tick += 1
    stats["score_for"] += w.score[our]
    stats["score_against"] += w.score[1 - our]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--matches", type=int, default=20)
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", help="write the full stats here")
    args = ap.parse_args()

    loop = asyncio.new_event_loop()
    handlers = build_team()
    stats = {"inference": Counter(), "situations": Counter(), "sources": Counter(), "commands": Counter(),
             "chasers": Counter(), "possession_truth": Counter(), "invalid": [], "incompatible": [],
             "score_for": 0, "score_against": 0}
    for m in range(args.matches):
        play(args.seed + m, args.ticks, handlers, loop, stats)

    n_ticks = args.matches * args.ticks
    print(f"SIMULATED: {args.matches} matches x {args.ticks} ticks = {n_ticks} ticks, {n_ticks * 5} handler calls "
          f"(toy physics, scripted opponent, no model)\n")
    print(f"invalid handler outputs : {len(stats['invalid'])}")
    print(f"incompatible actions    : {len(stats['incompatible'])}")
    print("\npossession inference (truth -> what our agents see):")
    for k, v in sorted(stats["inference"].items()):
        flag = "" if k.split("->")[0] == k.split("->")[1] else "   <-- disagrees"
        print(f"   {k:22s} {v:6d}  {v / n_ticks:6.1%}{flag}")
    print("\ncandidate rule situations (share of ticks):")
    for k, v in stats["situations"].most_common():
        print(f"   {k:32s} {v:6d}  {v / n_ticks:6.1%}")
    print("\nreply sources:", dict(sorted(stats["sources"].items())))
    print("players sent to the ball per tick:", dict(sorted(stats["chasers"].items())))
    print(f"\nindicative only: goals {stats['score_for']}-{stats['score_against']}, "
          f"true possession {dict(stats['possession_truth'])}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({k: (dict(v) if isinstance(v, Counter) else v) for k, v in stats.items()}, f, indent=1)
    return 0 if not stats["invalid"] and not stats["incompatible"] else 1


if __name__ == "__main__":
    sys.exit(main())
