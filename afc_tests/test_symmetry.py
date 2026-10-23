"""Home/away symmetry of the whole pipeline, 2026-10-23.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

Requirement: as team 1 (other side of the pitch) the agents must behave exactly as they do as team 0. For real
server states from home matches we build the same situation as the server would send it to us as team 1
(pitch rotated 180 degrees, home/away swapped, teamId 1) and run all five handlers on both:
  - the model must receive the identical text;
  - every reply - from a rule, the fallback, or a fixed model answer - must be the same command, rotated back
    (MOVE_TO negated, FOLLOW_PLAYER team swapped, teamId 1, every other parameter identical).
"""

import copy
import json
import os
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from fallback import DEF_CONFIG, FWD1_CONFIG, FWD2_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback
from rules import (carrier_shoot, chain, def_shape_on_our_ball, fwd1_support, fwd2_poacher, gk_distribute,
                   gk_loose_ball_in_box, own_half_release)
from test_afc_tactics import run

FIX = os.path.join(base.ROOT, "afc_tests", "fixtures")
with open(os.path.join(FIX, "real_states_20261022.json"), encoding="utf-8") as f:
    HOME = [c["gameState"] for c in json.load(f) if c["gameState"]["playMode"] == "OPEN_PLAY"]
with open(os.path.join(FIX, "real_states_gk_distribution_20261022.json"), encoding="utf-8") as f:
    HOME += [c["gameState"] for c in json.load(f)]

# exactly as wired in each ai-*/src/main.py
AGENTS = {0: ("GK", GK_CONFIG, chain(gk_loose_ball_in_box, gk_distribute)),
          1: ("DEF", DEF_CONFIG, chain(own_half_release, carrier_shoot, def_shape_on_our_ball)),
          2: ("MID", MID_CONFIG, chain(own_half_release, carrier_shoot)),
          3: ("FWD1", FWD1_CONFIG, chain(own_half_release, carrier_shoot, fwd1_support)),
          4: ("FWD2", FWD2_CONFIG, chain(own_half_release, carrier_shoot, fwd2_poacher))}

REPLIES = {
    "move": '[{"commandType":"MOVE_TO","playerId":%d,"parameters":{"target_x":20,"target_y":-7,"sprint":true},"duration":0}]',
    "press": '[{"commandType":"PRESS_BALL","playerId":%d,"parameters":{"intensity":0.7},"duration":3}]',
    "follow": '[{"commandType":"FOLLOW_PLAYER","playerId":%d,"parameters":{"target_player_id":3,"target_team":"AWAY","distance":5},"duration":3}]',
    "mark": '[{"commandType":"MARK","playerId":%d,"parameters":{"target_player_id":2,"tightness":"TIGHT"},"duration":3}]',
    "pass": '[{"commandType":"PASS","playerId":%d,"parameters":{"target_player_id":4,"type":"GROUND"},"duration":0}]',
    "shoot": '[{"commandType":"SHOOT","playerId":%d,"parameters":{"aim_location":"TL","power":0.8},"duration":0}]',
    "unusable": "(no usable answer)",
}


def as_away(gs):
    """The same real situation as the server would send it to us as team 1."""
    g = copy.deepcopy(gs)
    for p in g["players"]:
        p["position"] = {k: (-v if k in ("x", "y") else v) for k, v in p["position"].items()}
        if "velocity" in p:
            p["velocity"] = {k: (-v if k in ("x", "y") else v) for k, v in p["velocity"].items()}
        p["teamCode"] = {"home": "away", "away": "home"}[p["teamCode"]]
    for key in ("position", "velocity"):
        if key in g["ball"]:
            g["ball"][key] = {k: (-v if k in ("x", "z") else v) for k, v in g["ball"][key].items()}  # server ball: z lateral
    g["score"] = {"home": g["score"]["away"], "away": g["score"]["home"]}
    if g.get("modeTeamId") in (0, 1):
        g["modeTeamId"] = 1 - g["modeTeamId"]
    return g


def rotated_back(home_cmd):
    """What the away handler must send for the same decision."""
    c = copy.deepcopy(home_cmd)
    par = c.get("parameters") or {}
    if c["commandType"] == "MOVE_TO":
        par["target_x"], par["target_y"] = -par["target_x"], -par["target_y"]
    if c["commandType"] == "FOLLOW_PLAYER" and par.get("target_team") in ("HOME", "AWAY"):
        par["target_team"] = {"HOME": "AWAY", "AWAY": "HOME"}[par["target_team"]]
    c["teamId"] = 1
    return c


def norm(c):
    c = copy.deepcopy(c)
    for k, v in (c.get("parameters") or {}).items():
        if isinstance(v, float):
            c["parameters"][k] = round(v, 2) + 0.0  # -0.0 == 0.0
    return c


class TestHomeAwaySymmetry(unittest.TestCase):

    def check(self, reply_name):
        mismatches = []
        for pid, (label, cfg, rule) in AGENTS.items():
            reply = REPLIES[reply_name] % pid if "%d" in REPLIES[reply_name] else REPLIES[reply_name]
            for gs in HOME:
                outs, seen = [], []
                for state, team in ((gs, 0), (as_away(gs), 1)):
                    app, agent = base.App(), base.Agent(reply=reply)
                    handler = agent_base.create_invoke_handler(app, agent, pid, label, build_fallback(cfg), cfg, rule_fn=rule)
                    outs.append(run(handler, state, team, pid))
                    seen.append(agent.calls)
                if seen[0] != seen[1]:
                    mismatches.append((label, gs["tick"], "model input differs"))
                expected = [norm(rotated_back(c)) for c in outs[0]]
                if expected != [norm(c) for c in outs[1]]:
                    mismatches.append((label, gs["tick"], outs[0], outs[1]))
        self.assertEqual(mismatches, [], f"{len(mismatches)} mismatches with reply '{reply_name}': {mismatches[:3]}")

    def test_rules_and_fallback(self):
        self.check("unusable")

    def test_model_move(self):
        self.check("move")

    def test_model_press(self):
        self.check("press")

    def test_model_follow(self):
        self.check("follow")

    def test_model_mark(self):
        self.check("mark")

    def test_model_pass(self):
        self.check("pass")

    def test_model_shoot(self):
        self.check("shoot")

    def test_the_away_state_is_really_mirrored(self):
        gs = HOME[0]
        away = as_away(gs)
        ours_home = next(p for p in gs["players"] if p["teamCode"] == "home" and p["agentId"] == "agentId_0")
        ours_away = next(p for p in away["players"] if p["teamCode"] == "away" and p["agentId"] == "agentId_0")
        self.assertEqual(ours_away["position"]["x"], -ours_home["position"]["x"])
        self.assertGreater(ours_away["position"]["x"], 0)  # as team 1 our keeper stands at +x, as the server sent it


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
