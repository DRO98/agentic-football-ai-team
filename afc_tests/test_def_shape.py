"""DEF shape change of 2026-10-22: the evaluated prompt, the our-ball rule, and the eval's defending set.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

  1. prompt: ai-def's SYSTEM_PROMPT is the "shape" variant evaluated in model-eval-def-20261022-124954
  2. rule:   an outfield teammate on our ball -> DEF MOVE_TO (y 0) 15 behind the most advanced
             outfield teammate, never past halfway, without calling the model
  3. tool:   model_eval --scenario-set defending (8 situations) and its DEF heuristics
"""

import json
import os
import sys
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from action_validator import context_from_state, validate_reply
from fallback import DEF_CONFIG, build_fallback
from rules import DEF_SHAPE_OFFSET, def_shape_on_our_ball
from test_afc_integration import state_with
from test_afc_tactics import GK_HOLDS, LOOSE_IN_BOX, run
from test_helpers import GAME_STATE

sys.path.insert(0, os.path.join(base.ROOT, "afc_tools"))
import model_eval as me  # noqa: E402

TEAM = os.path.join(base.ROOT, "ai-team-strands-balanced")
FIX = os.path.join(base.ROOT, "afc_tests", "fixtures", "prompts")
VARIANTS = os.path.join(base.ROOT, "afc_tools", "prompt_variants")

# GAME_STATE players: index 0-4 home #0-#4, 5-9 away #0-#4.
OUR_ATTACK_IN_OUR_HALF = state_with("agentId_2", ball=(-19.5, 0), moves=[(2, (-20, 0)), (3, (-25, 5)), (4, (-30, -10))])
AWAY_OUR_BALL = state_with("agentId_3", ball=(-29.5, -6), moves=[(8, (-30, -6))])  # as team 1: away #3 holds it
THEIRS_NEAR_GOAL = state_with("agentId_3", ball=(-27.5, -6), moves=[(8, (-28, -6))])
DEF_HOLDS = state_with("agentId_1", ball=(-9.5, 12))


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


def move(cmds):
    (c,) = cmds
    return c["commandType"], c["parameters"]["target_x"], c["parameters"]["target_y"], c["parameters"]["sprint"]


class TestPromptIsTheEvaluatedOne(unittest.TestCase):

    def test_def_prompt_is_the_shape_variant_as_evaluated(self):
        self.assertEqual(me.load_prompt("def"), fixture("def_evaluated_shape.txt"))

    def test_evaluated_text_is_the_variant_file_applied_to_the_previous_prompt(self):
        spec = me.load_variant_file(os.path.join(VARIANTS, "def_shape.json"))
        self.assertEqual(me.apply_variant(fixture("def_pre_20261022.txt"), spec), fixture("def_evaluated_shape.txt"))

    def test_the_spec_variant_still_applies_to_the_previous_prompt(self):
        spec = me.load_variant_file(os.path.join(VARIANTS, "def_shape_spec.json"))
        self.assertIn("about 15 units closer", me.apply_variant(fixture("def_pre_20261022.txt"), spec))

    def test_def_main_wires_the_rule(self):
        with open(os.path.join(TEAM, "ai-def", "src", "main.py"), encoding="utf-8") as f:
            self.assertIn("rule_fn=chain(own_half_release, carrier_shoot, def_shape_on_our_ball)", f.read())

    def test_no_other_agent_got_the_def_text(self):
        for agent in ("ai-gk", "ai-mid", "ai-fwd1", "ai-fwd2"):
            with open(os.path.join(TEAM, agent, "src", "main.py"), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("def_shape_on_our_ball", src, agent)
            self.assertNotIn("distToMyGoal is under 35", src, agent)


class TestDefShapeRule(unittest.TestCase):

    def test_sample_attack_is_capped_at_halfway(self):
        # our #3 holds at x 14, most advanced outfield teammate #4 at x 20 -> 20 - 15 = 5 -> capped to 0
        self.assertEqual(move(def_shape_on_our_ball(GAME_STATE, 0, 1)), ("MOVE_TO", 0.0, 0.0, False))

    def test_attack_in_our_half_sits_15_behind(self):
        self.assertEqual(move(def_shape_on_our_ball(OUR_ATTACK_IN_OUR_HALF, 0, 1)),
                         ("MOVE_TO", -20 - DEF_SHAPE_OFFSET, 0.0, False))

    def test_away_team_mirrors_the_sign(self):
        # team 1 attacks -x: most advanced is #3 at x 20 (our own half) -> 20 + 15 = 35
        s = state_with("agentId_3", ball=(19.5, -6), moves=[(8, (20, -6)), (3, (0, 20))])
        self.assertEqual(move(def_shape_on_our_ball(s, 1, 1)), ("MOVE_TO", 35.0, 0.0, False))
        # most advanced #3 at x -10 -> -10 + 15 = 5, still our half for team 1
        s = state_with("agentId_3", ball=(-9.5, -6), moves=[(8, (-10, -6)), (6, (40, 0)), (7, (40, 5)), (9, (40, -5))])
        self.assertEqual(move(def_shape_on_our_ball(s, 1, 1)), ("MOVE_TO", 5.0, 0.0, False))

    def test_away_team_is_capped_at_halfway_too(self):
        # most advanced #3 at x -30 -> -15 is past halfway for team 1 (x < 0) -> 0
        self.assertEqual(move(def_shape_on_our_ball(AWAY_OUR_BALL, 1, 1)), ("MOVE_TO", 0.0, 0.0, False))

    def test_does_not_fire_otherwise(self):
        cases = {
            "not the DEF": (GAME_STATE, 0, 2),
            "the GK": (GAME_STATE, 0, 0),
            "their ball": (THEIRS_NEAR_GOAL, 0, 1),
            "sample as away team (their ball)": (GAME_STATE, 1, 1),
            "loose ball": (LOOSE_IN_BOX, 0, 1),
            "ambiguous holder": (state_with("agentId_0"), 0, 1),
            "our GK holds it": (GK_HOLDS, 0, 1),
            "DEF itself holds it": (DEF_HOLDS, 0, 1),
            "empty state": ({}, 0, 1),
        }
        for name, (s, team, pid) in cases.items():
            with self.subTest(name):
                self.assertIsNone(def_shape_on_our_ball(s, team, pid))

    def test_no_usable_teammate_position_means_no_rule(self):
        s = state_with("agentId_3", ball=(15.3, -5.2), moves=[(2, (None, 0)), (4, ("x", 0))])
        s["players"][3]["position"] = {"x": float("nan"), "y": 0}
        self.assertIsNone(def_shape_on_our_ball(s, 0, 1))

    def test_output_passes_the_validator(self):
        for s, team in ((GAME_STATE, 0), (OUR_ATTACK_IN_OUR_HALF, 0), (AWAY_OUR_BALL, 1)):
            cmds = def_shape_on_our_ball(s, team, 1)
            self.assertTrue(validate_reply(cmds, context_from_state(s, team, 1)).ok)

    def test_handler_answers_without_the_model(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        handler = agent_base.create_invoke_handler(app, agent, 1, "DEF", build_fallback(DEF_CONFIG), DEF_CONFIG,
                                                   rule_fn=def_shape_on_our_ball)
        self.assertEqual(run(handler, GAME_STATE, 0, 1), def_shape_on_our_ball(GAME_STATE, 0, 1))
        self.assertEqual(agent.calls, [])
        self.assertTrue(any("answered by rule def_shape_on_our_ball" in m for _, m in app.records))

    def test_handler_asks_the_model_on_their_ball(self):
        reply = '[{"commandType":"MARK","playerId":1,"parameters":{"target_player_id":3,"tightness":"TIGHT"},"duration":3}]'
        app, agent = base.App(), base.Agent(reply=reply)
        handler = agent_base.create_invoke_handler(app, agent, 1, "DEF", build_fallback(DEF_CONFIG), DEF_CONFIG,
                                                   rule_fn=def_shape_on_our_ball)
        self.assertEqual(run(handler, THEIRS_NEAR_GOAL, 0, 1)[0]["commandType"], "MARK")
        self.assertEqual(len(agent.calls), 1)


class TestDefendingScenarioSet(unittest.TestCase):

    EXPECT = {"our_mid_on_ball": ("OURS", 2), "our_fwd_wide": ("OURS", 4), "our_fwd_in_their_corner": ("OURS", 4),
              "our_fwd_deep_as_away": ("OURS", 3), "their_ball_in_their_corner": ("THEIRS", 4),
              "their_ball_wide_their_half": ("THEIRS", 2), "their_ball_our_sideline": ("THEIRS", 4),
              "their_ball_near_our_goal": ("THEIRS", 3)}

    def setUp(self):
        self.plan = me.build_plan("def", ["lite"], ["current"], 1, scenario_set="defending")

    def test_eight_situations_with_the_designed_possession(self):
        self.assertEqual([n for n, _, _ in self.plan["scenarios"]], list(self.EXPECT))
        for name, team, data in self.plan["scenarios"]:
            gs = data["gameState"]
            info = me.resolve_possession(gs["ball"], gs["players"], team)
            self.assertEqual((info.state.name, info.holder_idx), self.EXPECT[name], name)

    def scenario(self, name):
        return next((t, d["gameState"]) for n, t, d in self.plan["scenarios"] if n == name)

    def reply(self, cmd, params):
        return json.dumps([{"commandType": cmd, "playerId": 1, "parameters": params, "duration": 0}])

    def test_slide_in_their_corner_is_outside_the_zone(self):
        team, gs = self.scenario("their_ball_in_their_corner")
        row = me.score_reply(self.reply("SLIDE_TACKLE", {"target_player_id": 4, "sprint": True, "distance": 3}), gs, team, 1, DEF_CONFIG)
        self.assertTrue(row["tactical"]["slide_outside_zone"])

    def test_slide_near_our_goal_is_inside_the_zone(self):
        team, gs = self.scenario("their_ball_near_our_goal")
        row = me.score_reply(self.reply("SLIDE_TACKLE", {"target_player_id": 3, "sprint": True, "distance": 3}), gs, team, 1, DEF_CONFIG)
        self.assertFalse(row["tactical"]["slide_outside_zone"])

    def test_slide_on_our_sideline_is_outside_the_zone(self):
        team, gs = self.scenario("their_ball_our_sideline")
        row = me.score_reply(self.reply("SLIDE_TACKLE", {"target_player_id": 4, "sprint": True, "distance": 3}), gs, team, 1, DEF_CONFIG)
        self.assertTrue(row["tactical"]["slide_outside_zone"])

    def test_mark_in_their_half_is_a_chase(self):
        team, gs = self.scenario("their_ball_wide_their_half")
        row = me.score_reply(self.reply("MARK", {"target_player_id": 2, "tightness": "TIGHT"}), gs, team, 1, DEF_CONFIG)
        self.assertTrue(row["tactical"]["chase_opp_half"])

    def test_our_ball_shape_flags(self):
        team, gs = self.scenario("our_fwd_in_their_corner")  # most advanced: our #4 at x 50
        t = me.score_reply(self.reply("MOVE_TO", {"target_x": 50, "target_y": 0, "sprint": False}), gs, team, 1, DEF_CONFIG)["tactical"]
        self.assertEqual((t["shapeBehind"], t["shape_past_halfway"], t["shape_wide"]), (0, True, False))
        t = me.score_reply(self.reply("MOVE_TO", {"target_x": 0, "target_y": 20, "sprint": False}), gs, team, 1, DEF_CONFIG)["tactical"]
        self.assertEqual((t["shapeBehind"], t["shape_past_halfway"], t["shape_wide"]), (50, False, True))

    def test_away_shape_measures_toward_minus_x(self):
        team, gs = self.scenario("our_fwd_deep_as_away")  # most advanced: away #3 at x -40
        t = me.score_reply(self.reply("MOVE_TO", {"target_x": -25, "target_y": 0, "sprint": False}), gs, team, 1, DEF_CONFIG)["tactical"]
        self.assertEqual((t["shapeBehind"], t["shape_past_halfway"]), (15, True))

    def test_the_rule_answers_every_our_ball_situation_in_the_set(self):
        for name, team, data in self.plan["scenarios"]:
            fired = def_shape_on_our_ball(data["gameState"], team, 1) is not None
            self.assertEqual(fired, self.EXPECT[name][0] == "OURS", name)


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
