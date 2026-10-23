"""Own-half release (lib/rules.own_half_release) and the anti-backward filter (no_backward_carry), 2026-10-23.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

User request after match 19: in our half, do not carry (and never backwards); pass up to a free teammate
ahead, or with nobody free ahead and 2+ opponents in front, shoot hard at the top corner.
"""

import copy
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from action_validator import context_from_state, validate_reply
from fallback import MID_CONFIG, build_fallback
from rules import carrier_shoot, chain, no_backward_carry, own_half_release
from test_afc_integration import state_with
from test_afc_tactics import run

# GAME_STATE: index 0-4 home #0-#4, 5-9 away #0-#4. Our MID #2 holds the ball at (-20, 0) in our half.
MID_HOLDS = [(2, (-20, 0))]
FAR = [(6, (45, 30)), (7, (45, -30)), (8, (50, 25)), (9, (50, -25))]   # opponents' outfield far away


def holds(moves):
    return state_with("agentId_2", ball=(-19.5, 0), moves=MID_HOLDS + moves)


def cmd(cmds):
    (c,) = cmds
    return c["commandType"], c["parameters"]


class TestRelease(unittest.TestCase):

    def test_pass_to_the_most_advanced_free_teammate_ahead(self):
        s = holds(FAR)  # #1 (-10,12), #3 (14,-5), #4 (20,15) all free and ahead
        t, p = cmd(own_half_release(s, 0, 2))
        self.assertEqual((t, p), ("PASS", {"target_player_id": 4, "type": "AERIAL"}))
        self.assertTrue(validate_reply(own_half_release(s, 0, 2), context_from_state(s, 0, 2)).ok)

    def test_near_free_teammate_gets_a_ground_pass(self):
        s = holds(FAR + [(3, (-5, -3)), (4, (-8, 8)), (1, (-30, 0))])  # #3 ahead at 15
        self.assertEqual(cmd(own_half_release(s, 0, 2))[1], {"target_player_id": 3, "type": "GROUND"})

    def test_nobody_free_ahead_and_opponents_in_front_means_a_hard_long_shot(self):
        # markers on #1, #3, #4; their keeper (away #0) at (50, 0) in goal
        s = holds([(6, (-10, 13)), (8, (14, -4)), (9, (20, 14)), (7, (0, 0))])
        t, p = cmd(own_half_release(s, 0, 2))
        self.assertEqual(t, "SHOOT")
        self.assertEqual(p["power"], 1.0)
        self.assertIn(p["aim_location"], ("TL", "TR"))

    def test_top_corner_away_from_the_keeper_and_center_on_an_empty_goal(self):
        marked = [(6, (-10, 13)), (8, (14, -4)), (9, (20, 14)), (7, (0, 0))]
        left = holds(marked + [(5, (52, 5))])     # keeper on +lateral = the shooter's left for team 0
        right = holds(marked + [(5, (52, -5))])
        empty = holds(marked + [(5, (0, 0))])
        self.assertEqual(cmd(own_half_release(left, 0, 2))[1]["aim_location"], "TR")
        self.assertEqual(cmd(own_half_release(right, 0, 2))[1]["aim_location"], "TL")
        self.assertEqual(cmd(own_half_release(empty, 0, 2))[1]["aim_location"], "CENTER")

    def test_one_opponent_in_front_and_nobody_free_leaves_it_to_the_model(self):
        # every teammate ahead marked by one opponent each would need 3; use one marker in front, rest behind
        s = holds([(6, (-40, 10)), (7, (-40, -10)), (8, (-45, 0)), (9, (-10, 13)),
                   (1, (-10, 12)), (3, (-25, 5)), (4, (-26, -5))])
        self.assertIsNone(own_half_release(s, 0, 2))

    def test_only_the_carrier_in_our_half_in_open_play(self):
        s = holds(FAR)
        cases = {"not the carrier": (s, 3), "GK": (s, 0), "carrier in their half": (state_with("agentId_3", ball=(14.5, -5)), 3)}
        for name, (st, pid) in cases.items():
            with self.subTest(name):
                self.assertIsNone(own_half_release(st, 0, pid))
        reset = copy.deepcopy(s)
        reset["playMode"] = "RESETTING"
        self.assertIsNone(own_half_release(reset, 0, 2))

    def test_away_team_mirrors(self):
        # as team 1 our MID (away #2) holds at (20, 0): our half is x > 0, forward is -x
        s = state_with("agentId_2", ball=(19.5, 0), moves=[(7, (20, 0)), (2, (40, 25)), (1, (45, -25)), (3, (50, 20)), (4, (50, -20)),
                                                            (6, (-10, 0)), (8, (-20, 10)), (9, (0, -10))])
        t, p = cmd(own_half_release(s, 1, 2))
        self.assertEqual(t, "PASS")
        self.assertIn(p["target_player_id"], (1, 3, 4))


class TestNoBackwardCarry(unittest.TestCase):

    def move(self, x, y=0.0):
        return [{"commandType": "MOVE_TO", "playerId": 2, "teamId": 0, "parameters": {"target_x": x, "target_y": y, "sprint": True}}]

    def test_backward_carry_in_our_half_becomes_a_step_forward(self):
        out = no_backward_carry(self.move(-40, 10), holds(FAR), 0, 2)
        self.assertEqual(out[0]["parameters"], {"target_x": -10.0, "target_y": 0.0, "sprint": False})

    def test_forward_or_sideways_moves_are_kept(self):
        for x in (-10, -22):
            cmds = self.move(x, 5)
            self.assertEqual(no_backward_carry(cmds, holds(FAR), 0, 2), cmds)

    def test_not_carrying_or_their_half_is_untouched(self):
        cmds = self.move(-40)
        self.assertIs(no_backward_carry(cmds, holds(FAR), 0, 3), cmds)
        theirs = state_with("agentId_2", ball=(10.5, 0), moves=[(2, (10, 0))])
        self.assertIs(no_backward_carry(cmds, theirs, 0, 2), cmds)

    def test_handler_applies_it_to_the_model_reply(self):
        s = state_with("agentId_2", ball=(-19.5, 0), moves=MID_HOLDS + [(1, (-30, 5)), (3, (-35, 0)), (4, (-40, -5))]
                       + [(6, (-28, 6)), (8, (-33, 1)), (9, (-38, -4))])   # nobody ahead, 1 opponent in front -> model
        self.assertIsNone(own_half_release(s, 0, 2))
        reply = '[{"commandType":"MOVE_TO","playerId":2,"parameters":{"target_x":-45,"target_y":0,"sprint":true},"duration":0}]'
        app, agent = base.App(), base.Agent(reply=reply)
        handler = agent_base.create_invoke_handler(app, agent, 2, "MID", build_fallback(MID_CONFIG), MID_CONFIG,
                                                   rule_fn=chain(own_half_release, carrier_shoot))
        t, p = cmd(run(handler, s, 0, 2))
        self.assertEqual((t, p["target_x"], p["sprint"]), ("MOVE_TO", -10.0, False))
        self.assertEqual(len(agent.calls), 1)


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
