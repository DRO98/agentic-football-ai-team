"""Rules added after matches 2 and 3 (2026-10-22): carrier_shoot, fwd2_poacher, gk_leash, chain.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).
"""

import copy
import json
import os
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from action_validator import context_from_state, validate_reply
from fallback import FWD2_CONFIG, GK_CONFIG, build_fallback
from frame import to_design_frame
from possession import Possession, resolve_possession
from rules import (GK_MAX_DEPTH, SHOOT_RANGE, carrier_shoot, chain, def_shape_on_our_ball, fwd1_support,
                   fwd2_poacher, gk_leash)
from test_afc_integration import state_with
from test_afc_tactics import run
from test_helpers import GAME_STATE

TEAM = os.path.join(base.ROOT, "ai-team-strands-balanced")
with open(os.path.join(base.ROOT, "afc_tests", "fixtures", "real_states_20261022.json"), encoding="utf-8") as f:
    REAL = [to_design_frame(c["gameState"])[0] for c in json.load(f)]

# GAME_STATE: index 0-4 home #0-#4, 5-9 away #0-#4. Sample: our #3 holds at x 14 (41 from their goal).
FWD1_HOLDS_DEEP = state_with("agentId_3", ball=(-19.5, -5), moves=[(3, (-20, -5))])  # 75 from their goal
DEF_HOLDS_UPFIELD = state_with("agentId_1", ball=(30.5, 2), moves=[(1, (30, 2))])
THEIRS_FAR = state_with("agentId_3", ball=(-27.5, -6), moves=[(8, (-28, -6))])
THEIRS_NEAR_FWD2 = state_with("agentId_2", ball=(20.5, 12), moves=[(7, (21, 12))])  # our #4 sits at (20, 15)


def cmd(cmds):
    (c,) = cmds
    return c["commandType"], c["parameters"]


class TestCarrierShoot(unittest.TestCase):

    def test_holder_in_their_half_shoots(self):
        t, p = cmd(carrier_shoot(GAME_STATE, 0, 3))
        self.assertEqual(t, "SHOOT")
        self.assertEqual(p["aim_location"], "CENTER")
        self.assertTrue(validate_reply(carrier_shoot(GAME_STATE, 0, 3), context_from_state(GAME_STATE, 0, 3)).ok)

    def test_any_outfield_player_shoots_even_the_defender(self):
        self.assertEqual(cmd(carrier_shoot(DEF_HOLDS_UPFIELD, 0, 1))[0], "SHOOT")

    def test_no_shot_from_our_half_or_without_the_ball(self):
        cases = {"holder deep in our half": (FWD1_HOLDS_DEEP, 3), "teammate holds": (GAME_STATE, 4),
                 "their ball": (THEIRS_FAR, 3), "GK never": (state_with("agentId_0", ball=(-50, 0)), 0),
                 "empty": ({}, 3)}
        for name, (s, pid) in cases.items():
            with self.subTest(name):
                self.assertIsNone(carrier_shoot(s, 0, pid))

    def test_range_is_their_half(self):
        self.assertEqual(SHOOT_RANGE, 55.0)

    def test_away_team_shoots_toward_minus_x(self):
        s = state_with("agentId_3", ball=(-19.5, -6), moves=[(8, (-20, -6)), (3, (0, 25))])  # away #3, 35 from x=-55
        self.assertEqual(cmd(carrier_shoot(s, 1, 3))[0], "SHOOT")
        s = state_with("agentId_3", ball=(19.5, -6), moves=[(8, (20, -6)), (3, (0, 25))])    # away #3 in its own half
        self.assertIsNone(carrier_shoot(s, 1, 3))


class TestShotAngleAndAim(unittest.TestCase):
    """Match 4: narrow goal (every goal within 0.8 m = ~7 design units of the centre), many shots at the post."""

    def test_tight_angle_cuts_inside_instead_of_shooting(self):
        s = state_with("agentId_3", ball=(45.5, 28), moves=[(3, (45, 28))])  # 10 from their line, 28 out wide
        t, p = cmd(carrier_shoot(s, 0, 3))
        self.assertEqual((t, p["target_x"], p["target_y"], p["sprint"]), ("MOVE_TO", 51.0, 11.2, False))
        self.assertTrue(validate_reply(carrier_shoot(s, 0, 3), context_from_state(s, 0, 3)).ok)

    def test_wide_but_far_enough_still_shoots(self):
        s = state_with("agentId_3", ball=(15.5, 20), moves=[(3, (15, 20))])  # 40 out, 20 wide: 20 <= 7 + 20
        self.assertEqual(cmd(carrier_shoot(s, 0, 3))[0], "SHOOT")

    def test_keeper_off_centre_means_center(self):
        s = state_with("agentId_3", ball=(14.5, -5), moves=[(3, (14, -5)), (5, (50, 6))])
        self.assertEqual(cmd(carrier_shoot(s, 0, 3))[1]["aim_location"], "CENTER")

    def test_centred_keeper_is_center_too(self):
        # matches 4-11: CENTER 4/21 goals, BL/BR 1/17 (corners ended 2.8-3.6 m wide)
        a, b = copy.deepcopy(GAME_STATE), copy.deepcopy(GAME_STATE)
        b["tick"] = a["tick"] + 1
        self.assertEqual({cmd(carrier_shoot(a, 0, 3))[1]["aim_location"], cmd(carrier_shoot(b, 0, 3))[1]["aim_location"]},
                         {"CENTER"})

    def test_rule_answer_logs_its_parameters(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        handler = agent_base.create_invoke_handler(app, agent, 3, "FWD1", build_fallback(FWD2_CONFIG), FWD2_CONFIG,
                                                   rule_fn=carrier_shoot)
        run(handler, GAME_STATE, 0, 3)
        line = next(m for _, m in app.records if "answered by rule carrier_shoot" in m)
        self.assertIn('"aim_location"', line)


class TestEmptyGoal(unittest.TestCase):
    """Match 6: their #0 left its goal on about half the ticks; corner shots with it at halfway went wide."""

    def test_keeper_out_and_far_dribbles_toward_goal_first(self):
        # match 14: empty-goal shots from 8-10 m 0/3 -> beyond 60 design units, advance first
        s = state_with("agentId_3", ball=(-19.5, -5), moves=[(3, (-20, -5)), (5, (-50, 0))])  # their #0 on OUR line
        t, p = cmd(carrier_shoot(s, 0, 3))
        self.assertEqual((t, p["target_x"], p["target_y"], p["sprint"]), ("MOVE_TO", -8.0, -2.5, False))
        self.assertTrue(validate_reply(carrier_shoot(s, 0, 3), context_from_state(s, 0, 3)).ok)

    def test_keeper_out_and_close_enough_shoots_center(self):
        s = state_with("agentId_3", ball=(0.5, -5), moves=[(3, (0, -5)), (5, (-50, 0))])  # 55 from their line
        t, p = cmd(carrier_shoot(s, 0, 3))
        self.assertEqual((t, p["aim_location"], p["power"]), ("SHOOT", "CENTER", 0.85))

    def test_keeper_out_but_too_far_does_not_shoot(self):
        s = state_with("agentId_3", ball=(-30.5, -5), moves=[(3, (-31, -5)), (5, (0, 0))])  # 86 from their goal
        self.assertIsNone(carrier_shoot(s, 0, 3))

    def test_keeper_near_halfway_and_centred_is_not_a_corner_shot(self):
        s = state_with("agentId_3", ball=(32.5, -1), moves=[(3, (32, -1)), (5, (12, 0))])  # the match-6 case
        self.assertEqual(cmd(carrier_shoot(s, 0, 3))[1]["aim_location"], "CENTER")

    def test_keeper_in_goal_keeps_the_their_half_rule(self):
        self.assertIsNone(carrier_shoot(FWD1_HOLDS_DEEP, 0, 3))  # sample keeper at x 50


class TestShotPowerAndPlayMode(unittest.TestCase):
    """Match 7: power 1.0 went over the bar; shots fired from the centre spot at kick-off/reset."""

    def test_power_grows_with_distance(self):
        from rules import shot_power
        self.assertEqual([shot_power(d) for d in (10, 30, 31, 55, 56, 80)], [0.75, 0.75, 0.85, 0.85, 0.95, 0.95])
        self.assertEqual(cmd(carrier_shoot(GAME_STATE, 0, 3))[1]["power"], 0.85)  # sample: 41 from their line

    def test_no_shot_outside_open_play(self):
        for mode in ("KICK_OFF", "RESETTING", "GOAL_KICK"):
            s = copy.deepcopy(GAME_STATE)
            s["playMode"] = mode
            self.assertIsNone(carrier_shoot(s, 0, 3), mode)


class TestFwd1Support(unittest.TestCase):
    """Match 8: FWD1 pressed our own carrier (own_ball rejections) and ran itself to stamina 0.29."""

    def test_our_mid_on_the_ball_fwd1_goes_ahead_of_it(self):
        s = state_with("agentId_2", ball=(0.5, 2), moves=[(2, (0, 2))])
        t, p = cmd(fwd1_support(s, 0, 3))
        self.assertEqual((t, p["target_x"], p["target_y"]), ("MOVE_TO", 15.5, 1.0))
        self.assertTrue(validate_reply(fwd1_support(s, 0, 3), context_from_state(s, 0, 3)).ok)

    def test_never_past_the_cap_and_mirrored_for_away(self):
        s = state_with("agentId_4", ball=(30.5, 10), moves=[(4, (30, 10))])
        self.assertEqual(cmd(fwd1_support(s, 0, 3))[1]["target_x"], 33.0)
        s = state_with("agentId_3", ball=(-29.5, -6), moves=[(8, (-30, -6))])  # as team 1, our #3 (FWD1) holds it
        self.assertIsNone(fwd1_support(s, 1, 3))  # I hold it myself -> the shoot rule / model decides

    def test_does_not_fire_otherwise(self):
        cases = {"their ball": THEIRS_FAR, "loose": state_with(None, ball=(0, 0)), "our GK holds": state_with("agentId_0", ball=(-50, 0)),
                 "I hold it": GAME_STATE}
        for name, s in cases.items():
            with self.subTest(name):
                self.assertIsNone(fwd1_support(s, 0, 3))
        self.assertIsNone(fwd1_support(state_with("agentId_2", ball=(0.5, 2), moves=[(2, (0, 2))]), 0, 4))


class TestFarPostFromWide(unittest.TestCase):
    """2026-10-23: shots from out wide kept hitting the side netting -> aim the low far corner from there."""

    def test_between_the_posts_is_center(self):
        from rules import shot_aim
        self.assertEqual(shot_aim((40, 6.9), 1.0), "CENTER")
        self.assertEqual(shot_aim((40, -6.9), 1.0), "CENTER")

    def test_wide_left_aims_far_right_and_wide_right_aims_far_left(self):
        from rules import shot_aim
        self.assertEqual(shot_aim((40, 15), 1.0), "BR")    # team 0: +lateral is the shooter's left
        self.assertEqual(shot_aim((40, -15), 1.0), "BL")
        self.assertEqual(shot_aim((-40, -15), -1.0), "BR")  # team 1 facing -x: -lateral is the shooter's left
        self.assertEqual(shot_aim((-40, 15), -1.0), "BL")

    def test_rule_uses_it(self):
        s = state_with("agentId_3", ball=(35.5, 15), moves=[(3, (35, 15))])  # 20 out, 15 wide: angle ok (15 <= 17)
        t, p = cmd(carrier_shoot(s, 0, 3))
        self.assertEqual((t, p["aim_location"]), ("SHOOT", "BR"))


class TestPoacher(unittest.TestCase):

    def test_our_ball_gets_open_near_their_goal_away_from_the_ball(self):
        t, p = cmd(fwd2_poacher(GAME_STATE, 0, 4))  # ball at y -5.2 -> go to +y
        self.assertEqual((t, p["target_x"], p["target_y"]), ("MOVE_TO", 38.5, 12.0))

    def test_their_ball_far_waits_just_inside_their_half(self):
        t, p = cmd(fwd2_poacher(THEIRS_FAR, 0, 4))
        self.assertEqual((t, p["target_x"]), ("MOVE_TO", 11.0))

    def test_their_ball_near_or_loose_or_me_holding_goes_to_the_model(self):
        for name, s in {"their ball close": THEIRS_NEAR_FWD2, "loose": state_with(None, ball=(0, 0)),
                        "I hold it": state_with("agentId_4", ball=(20.5, 15))}.items():
            with self.subTest(name):
                self.assertIsNone(fwd2_poacher(s, 0, 4))

    def test_only_fwd2(self):
        for pid in (0, 1, 2, 3):
            self.assertIsNone(fwd2_poacher(GAME_STATE, 0, pid))

    def test_away_mirror(self):
        s = state_with("agentId_3", ball=(-29.5, -6), moves=[(8, (-30, -6))])  # away #3 holds -> our ball as team 1
        t, p = cmd(fwd2_poacher(s, 1, 4))
        self.assertEqual((t, p["target_x"], p["target_y"]), ("MOVE_TO", -38.5, 12.0))


class TestChain(unittest.TestCase):

    def test_first_answer_wins_and_names_the_rule(self):
        r = chain(carrier_shoot, fwd2_poacher)
        self.assertEqual(cmd(r(GAME_STATE, 0, 4))[0], "MOVE_TO")
        self.assertEqual(r.__name__, "fwd2_poacher")
        self.assertEqual(cmd(r(state_with("agentId_4", ball=(20.5, 15)), 0, 4))[0], "SHOOT")
        self.assertEqual(r.__name__, "carrier_shoot")
        self.assertIsNone(r(state_with(None, ball=(0, 0)), 0, 4))

    def test_def_chain_shoots_when_holding_and_holds_shape_otherwise(self):
        r = chain(carrier_shoot, def_shape_on_our_ball)
        self.assertEqual(cmd(r(DEF_HOLDS_UPFIELD, 0, 1))[0], "SHOOT")
        self.assertEqual(cmd(r(GAME_STATE, 0, 1)), ("MOVE_TO", {"target_x": 0.0, "target_y": 0.0, "sprint": False}))


class TestGkLeash(unittest.TestCase):

    def move(self, x, y):
        return [{"commandType": "MOVE_TO", "playerId": 0, "teamId": 0,
                 "parameters": {"target_x": x, "target_y": y, "sprint": False}, "duration": 0}]

    def test_target_upfield_is_pulled_back_to_the_box(self):
        p = gk_leash(self.move(55, 30), GAME_STATE, 0, 0)[0]["parameters"]
        self.assertEqual((p["target_x"], p["target_y"]), (-55 + GK_MAX_DEPTH, 20.16))
        p = gk_leash(self.move(-55, 30), GAME_STATE, 1, 0)[0]["parameters"]  # away GK defends +55
        self.assertEqual(p["target_x"], 55 - GK_MAX_DEPTH)

    def test_targets_in_the_box_and_other_players_are_untouched(self):
        cmds = self.move(-50, 2)
        self.assertEqual(gk_leash(cmds, GAME_STATE, 0, 0), cmds)
        far = self.move(55, 30)
        self.assertIs(gk_leash(far, GAME_STATE, 0, 1), far)

    def test_handler_leashes_the_real_failure(self):
        # match 3: the GK model sent MOVE_TO x 6.4 (the other goal) and the opponent scored from its own box
        g = next(c for c in REAL if resolve_possession(c["ball"], c["players"], 0).state is Possession.OURS)
        raw = next(c for c in json.load(open(os.path.join(base.ROOT, "afc_tests", "fixtures", "real_states_20261022.json"),
                                             encoding="utf-8"))
                   if to_design_frame(c["gameState"])[0] == g)["gameState"]
        reply = '[{"commandType":"MOVE_TO","playerId":0,"parameters":{"target_x":55,"target_y":0,"sprint":false},"duration":0}]'
        app, agent = base.App(), base.Agent(reply=reply)
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG)
        out = run(handler, raw, 0, 0)[0]["parameters"]
        self.assertLessEqual(out["target_x"], round((-55 + GK_MAX_DEPTH) / (55 / 6.4), 2))  # server units, own box


class TestOnRealStates(unittest.TestCase):

    def test_rules_answer_valid_commands_on_every_real_state(self):
        for g in REAL:
            for pid, rule in ((1, chain(carrier_shoot, def_shape_on_our_ball)), (2, carrier_shoot),
                              (3, chain(carrier_shoot, fwd1_support)), (4, chain(carrier_shoot, fwd2_poacher))):
                out = rule(g, 0, pid)
                if out:
                    with self.subTest(tick=g["tick"], pid=pid):
                        self.assertTrue(validate_reply(out, context_from_state(g, 0, pid)).ok, out)

    def test_wiring_in_main(self):
        expect = {"ai-def": "rule_fn=chain(own_half_release, carrier_shoot, def_shape_on_our_ball)", "ai-mid": "rule_fn=chain(own_half_release, carrier_shoot)",
                  "ai-fwd1": "rule_fn=chain(own_half_release, carrier_shoot, fwd1_support)", "ai-fwd2": "rule_fn=chain(own_half_release, carrier_shoot, fwd2_poacher)",
                  "ai-gk": "rule_fn=chain(gk_loose_ball_in_box, gk_distribute)"}
        for agent, text in expect.items():
            with open(os.path.join(TEAM, agent, "src", "main.py"), encoding="utf-8") as f:
                self.assertIn(text, f.read(), agent)

    def test_poacher_handler_on_a_real_state(self):
        g = next(c for c in json.load(open(os.path.join(base.ROOT, "afc_tests", "fixtures", "real_states_20261022.json"),
                                           encoding="utf-8")) if c["gameState"]["tick"] == 4780)["gameState"]
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        handler = agent_base.create_invoke_handler(app, agent, 4, "FWD2", build_fallback(FWD2_CONFIG), FWD2_CONFIG,
                                                   rule_fn=chain(carrier_shoot, fwd2_poacher))
        t, p = cmd(run(handler, g, 0, 4))
        self.assertEqual((t, p["target_x"]), ("MOVE_TO", round(38.5 / (55 / 6.4), 2)))
        self.assertEqual(agent.calls, [])


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
