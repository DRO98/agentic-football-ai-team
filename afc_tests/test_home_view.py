"""Team 1 plays a home view (lib/frame.to_home_view / from_home_view), 2026-10-22.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

Match 13 was our first as team 1 (lost 0-3) and the forwards ran backwards with our ball: on that match's
states Nova Lite sent FWD2 backwards 7/8 and FWD1 4/5 as team 1, but 2/8 and 2/4 on the same states rotated
into a home view. Fixture: afc_tests/fixtures/real_states_away_20261022.json (the 72 prompts of that match).
"""

import copy
import json
import os
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from fallback import DEF_CONFIG, FWD1_CONFIG, FWD2_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback
from frame import SCALE, from_home_view, to_design_frame, to_home_view, to_server_frame
from possession import resolve_possession
from rules import (carrier_shoot, chain, def_shape_on_our_ball, fwd1_support, fwd2_poacher, gk_distribute,
                   gk_loose_ball_in_box, own_half_release)
from test_afc_tactics import run
from test_helpers import GAME_STATE

with open(os.path.join(base.ROOT, "afc_tests", "fixtures", "real_states_away_20261022.json"), encoding="utf-8") as f:
    AWAY = json.load(f)
RULES = [(1, chain(own_half_release, carrier_shoot, def_shape_on_our_ball)), (2, chain(own_half_release, carrier_shoot)),
         (3, chain(own_half_release, carrier_shoot, fwd1_support)), (4, chain(own_half_release, carrier_shoot, fwd2_poacher)),
         (0, chain(gk_loose_ball_in_box, gk_distribute))]
CONFIGS = {0: ("GK", GK_CONFIG), 1: ("DEF", DEF_CONFIG), 2: ("MID", MID_CONFIG), 3: ("FWD1", FWD1_CONFIG), 4: ("FWD2", FWD2_CONFIG)}


class TestRotation(unittest.TestCase):

    def test_team_0_is_untouched(self):
        gs, flipped = to_home_view(GAME_STATE, 0)
        self.assertIs(gs, GAME_STATE)
        self.assertFalse(flipped)

    def test_team_1_is_rotated_and_relabelled(self):
        gs, flipped = to_home_view(GAME_STATE, 1)
        self.assertTrue(flipped)
        self.assertEqual(gs["players"][0], {**GAME_STATE["players"][0], "teamCode": "away",
                                            "position": {"x": 50, "y": 0}, "velocity": {"x": 0, "y": 0}})
        self.assertEqual(gs["ball"]["position"], {"x": -15.3, "y": 5.2, "z": 0})
        self.assertEqual(gs["score"], {"home": 0, "away": 1})

    def test_twice_is_identity_and_input_untouched(self):
        before = copy.deepcopy(GAME_STATE)
        once, _ = to_home_view(GAME_STATE, 1)
        twice, _ = to_home_view(once, 1)
        self.assertEqual(twice, GAME_STATE)
        self.assertEqual(GAME_STATE, before)

    def test_possession_is_preserved(self):
        for c in AWAY:
            d, _ = to_design_frame(c["gameState"])
            a = resolve_possession(d["ball"], d["players"], 1)
            v, _ = to_home_view(d, 1)
            b = resolve_possession(v["ball"], v["players"], 0)
            self.assertEqual((a.state, a.holder_idx), (b.state, b.holder_idx), c["gameState"]["tick"])

    def test_replies_are_rotated_back(self):
        cmds = [{"commandType": "MOVE_TO", "playerId": 3, "teamId": 0, "parameters": {"target_x": 25, "target_y": -5, "sprint": True}},
                {"commandType": "FOLLOW_PLAYER", "playerId": 3, "parameters": {"target_player_id": 2, "target_team": "AWAY", "distance": 3}},
                {"commandType": "PASS", "playerId": 3, "teamId": 0, "parameters": {"target_player_id": 4, "type": "GROUND"}}]
        out = from_home_view(cmds, True, 1)
        self.assertEqual(out[0]["parameters"], {"target_x": -25, "target_y": 5, "sprint": True})
        self.assertEqual(out[1]["parameters"]["target_team"], "HOME")
        self.assertEqual([c["teamId"] for c in out], [1, 1, 1])
        self.assertEqual(out[2]["parameters"], cmds[2]["parameters"])
        self.assertIs(from_home_view(cmds, False, 0), cmds)


class TestRulesAgreeOnRealAwayStates(unittest.TestCase):
    """The rules handle team 1 natively; through the home view they must give the same physical command."""

    def test_same_command_through_the_home_view(self):
        compared = 0
        for c in AWAY:
            d, scale = to_design_frame(c["gameState"])
            v, _ = to_home_view(d, 1)
            for pid, rule in RULES:
                direct = rule(d, 1, pid)
                viewed = rule(v, 0, pid)
                if direct is None:
                    self.assertIsNone(viewed, (c["gameState"]["tick"], pid))
                    continue
                compared += 1
                back = from_home_view(viewed, True, 1)
                for a, b in zip(to_server_frame(direct, scale), to_server_frame(back, scale)):
                    self.assertEqual(a["commandType"], b["commandType"])
                    for k, val in a["parameters"].items():
                        if isinstance(val, float):
                            self.assertAlmostEqual(val, b["parameters"][k], places=1, msg=(c["gameState"]["tick"], pid, k))
                        else:
                            self.assertEqual(val, b["parameters"][k], (c["gameState"]["tick"], pid, k))
        self.assertGreater(compared, 30)


class TestHandlerAsTeam1(unittest.TestCase):

    def state(self):
        return next(c for c in AWAY if c["gameState"]["playMode"] == "OPEN_PLAY")["gameState"]

    def test_model_forward_in_the_home_view_is_forward_for_team_1(self):
        reply = '[{"commandType":"MOVE_TO","playerId":3,"parameters":{"target_x":25,"target_y":5,"sprint":false},"duration":0}]'
        app, agent = base.App(), base.Agent(reply=reply)
        handler = agent_base.create_invoke_handler(app, agent, 3, "FWD1", build_fallback(FWD1_CONFIG), FWD1_CONFIG)
        (out,) = run(handler, self.state(), 1, 3)
        self.assertEqual(out["teamId"], 1)
        self.assertEqual((out["parameters"]["target_x"], out["parameters"]["target_y"]), (round(-25 / SCALE, 2), round(-5 / SCALE, 2)))
        self.assertIn("Team: 0 (HOME)", agent.calls[0])           # the model always sees a home view
        self.assertIn("Your goal at x=-55", agent.calls[0])
        logs = " ".join(m for _, m in app.records)
        self.assertIn("agent invoked for team 1", logs)          # logs keep the real team
        self.assertIn("(home view)", logs)

    def test_every_agent_on_every_away_state_answers_team_1_on_the_pitch(self):
        silent = base.Agent(reply="(no usable answer)")
        for pid, rule in RULES:
            label, cfg = CONFIGS[pid]
            app = base.App()
            handler = agent_base.create_invoke_handler(app, silent, pid, label, build_fallback(cfg), cfg, rule_fn=rule)
            for c in AWAY:
                (out,) = run(handler, c["gameState"], 1, pid)
                self.assertEqual(out["teamId"], 1, (label, c["gameState"]["tick"]))
                if out["commandType"] == "MOVE_TO":
                    self.assertLessEqual(abs(out["parameters"]["target_x"]), 6.4)
            self.assertFalse(any("last resort" in m or "agent error" in m for _, m in app.records), label)

    def test_poacher_goes_toward_minus_x_as_team_1(self):
        moves = []
        app, agent = base.App(), base.Agent(reply="(no usable answer)")
        handler = agent_base.create_invoke_handler(app, agent, 4, "FWD2", build_fallback(FWD2_CONFIG), FWD2_CONFIG,
                                                   rule_fn=chain(carrier_shoot, fwd2_poacher))
        for c in AWAY:
            d, _ = to_design_frame(c["gameState"])
            info = resolve_possession(d["ball"], d["players"], 1)
            if c["gameState"]["playMode"] == "OPEN_PLAY" and info.state.name == "OURS" and info.holder_idx not in (0, 4):
                (out,) = run(handler, c["gameState"], 1, 4)
                moves.append(out["parameters"]["target_x"])
        self.assertTrue(moves)
        self.assertTrue(all(x < 0 for x in moves), moves)  # team 1 attacks -x


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
