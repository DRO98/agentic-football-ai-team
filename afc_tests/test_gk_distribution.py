"""GK distribution rule (lib/rules.gk_distribute), 2026-10-22.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

Match 15: the GK threw 3/3 distributions to DEF #1 inside our box with opponents 0.2-0.6 m away and lost the
ball each time, while FWD2 stood free near halfway. Fixture: real_states_gk_distribution_20261022.json = the
three prompts the GK answered with those throws.
"""

import json
import os
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from action_validator import context_from_state, validate_reply
from fallback import GK_CONFIG, build_fallback
from frame import to_design_frame
from rules import OPEN_RADIUS, chain, gk_distribute, gk_loose_ball_in_box
from test_afc_integration import state_with
from test_afc_tactics import run
from test_helpers import GAME_STATE

with open(os.path.join(base.ROOT, "afc_tests", "fixtures", "real_states_gk_distribution_20261022.json"), encoding="utf-8") as f:
    REAL = json.load(f)
GK_HOLDS = state_with("agentId_0", ball=(-50, 0))  # sample positions: #1 (-10,12), #2 (5,-8), #3 (14,-5), #4 (20,15)


def cmd(cmds):
    (c,) = cmds
    return c["commandType"], c["parameters"]


class TestRealDistributions(unittest.TestCase):

    def test_never_the_marked_defender_in_our_box(self):
        for c in REAL:
            d, _ = to_design_frame(c["gameState"])
            t, p = cmd(gk_distribute(d, 0, 0))
            self.assertEqual(t, "GK_DISTRIBUTE")
            self.assertNotEqual(p["target_player_id"], 1, c["gameState"]["tick"])
            self.assertTrue(validate_reply(gk_distribute(d, 0, 0), context_from_state(d, 0, 0)).ok)

    def test_it_finds_the_free_forward_and_kicks_long(self):
        for c in REAL:
            d, _ = to_design_frame(c["gameState"])
            self.assertEqual(cmd(gk_distribute(d, 0, 0))[1], {"target_player_id": 4, "method": "KICK"}, c["gameState"]["tick"])


class TestChoice(unittest.TestCase):

    # opponents' outfield parked far away, so every teammate of ours is open
    FAR = [(6, (45, 30)), (7, (45, -30)), (8, (50, 25)), (9, (50, -25))]

    def test_sample_only_the_defender_is_open(self):
        # sample: #2, #3, #4 each have an opponent within 13; #1 at (-10, 12) is 25 from the nearest
        self.assertEqual(cmd(gk_distribute(GK_HOLDS, 0, 0))[1]["target_player_id"], 1)

    def test_most_advanced_open_teammate(self):
        s = state_with("agentId_0", ball=(-50, 0), moves=self.FAR)
        self.assertEqual(cmd(gk_distribute(s, 0, 0))[1], {"target_player_id": 4, "method": "KICK"})  # #4 at x 20

    def test_marked_teammates_are_skipped(self):
        # markers on #3 and #4; the one on #3 is also 9.8 from #2 (5, -8) -> only #1 (-10, 12) is open
        s = state_with("agentId_0", ball=(-50, 0), moves=self.FAR[:2] + [(8, (14, -4)), (9, (20, 14))])
        self.assertEqual(cmd(gk_distribute(s, 0, 0))[1]["target_player_id"], 1)

    def test_near_open_teammate_gets_a_throw(self):
        s = state_with("agentId_0", ball=(-50, 0), moves=[(1, (-30, 5)), (2, (-28, -8)), (3, (-25, 0)), (4, (-20, 10))])
        self.assertEqual(cmd(gk_distribute(s, 0, 0))[1], {"target_player_id": 4, "method": "THROW"})

    def test_nobody_open_means_least_marked_and_long(self):
        s = state_with("agentId_0", ball=(-50, 0), moves=[(6, (-10, 13)), (7, (5, -7)), (8, (14, -4)), (9, (20, 14))])
        t, p = cmd(gk_distribute(s, 0, 0))
        self.assertEqual(p["method"], "KICK")
        self.assertGreaterEqual(OPEN_RADIUS, 1)

    def test_only_when_our_gk_holds_it(self):
        for name, (s, pid) in {"not the GK": (GK_HOLDS, 1), "teammate holds": (GAME_STATE, 0),
                               "loose": (state_with(None, ball=(-45, 0)), 0), "empty": ({}, 0)}.items():
            with self.subTest(name):
                self.assertIsNone(gk_distribute(s, 0, pid))

    def test_away_team(self):
        s = state_with("agentId_0", ball=(50, 0), moves=[(5, (50, 0))])  # away #0 holds; GAME_STATE has away #0 at x 50
        t, p = cmd(gk_distribute(s, 1, 0))
        self.assertEqual(t, "GK_DISTRIBUTE")
        self.assertIn(p["target_player_id"], (1, 2, 3, 4))


class TestHandler(unittest.TestCase):

    def test_gk_main_chain_answers_without_the_model(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        rule = chain(gk_loose_ball_in_box, gk_distribute)
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG, rule_fn=rule)
        out = run(handler, REAL[0]["gameState"], 0, 0)
        self.assertEqual(cmd(out), ("GK_DISTRIBUTE", {"target_player_id": 4, "method": "KICK"}))
        self.assertEqual(agent.calls, [])
        self.assertTrue(any("answered by rule gk_distribute" in m for _, m in app.records))


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
