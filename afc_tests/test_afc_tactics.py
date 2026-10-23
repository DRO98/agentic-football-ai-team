"""AFC tactical fixes from the deployed team run of 2026-10-18 (afc_tools/reports/team-20261018-185239.json).
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

  1. validator: PRESS_BALL on our own ball is rejected (DEF and MID pressed while our GK held it)
  2. prompts:   all five say duration is a whole number >= 0 (FWD2 sent duration -1 twice)
  3. GK rule:   a loose ball in our penalty area -> INTERCEPT without calling the model
"""

import json
import os
import sys
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from action_validator import context_from_state, extract_reply, validate_reply
from fallback import DEF_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback
from rules import PENALTY_AREA_DEPTH, PENALTY_AREA_HALF_WIDTH, ball_in_own_penalty_area, gk_loose_ball_in_box
from test_afc_integration import state_with
from test_helpers import GAME_STATE

TEAM = os.path.join(base.ROOT, "ai-team-strands-balanced")
# The two real replies from the deployed run, scenario gk_holds_ball (our GK on the ball).
DEF_REAL = '[{"commandType":"PRESS_BALL","playerId":1,"parameters":{"intensity":0.8},"duration":5}]'
MID_REAL = '[{"commandType":"PRESS_BALL","playerId":2,"parameters":{"intensity":0.8},"duration":0}]'
GK_HOLDS = state_with("agentId_0", ball=(-50, 0))
LOOSE_IN_BOX = state_with(None, ball=(-42, 4))  # the team run's loose_ball_our_box scenario


def run(handler, state, team=0, pid=0):
    async def collect():
        return [c async for c in handler({"prompt": json.dumps({"gameState": state, "teamId": team,
                                                                "myPlayers": [pid]})}, None)]
    return json.loads(base.LOOP.run_until_complete(collect())[0])


class TestPressOwnBall(unittest.TestCase):

    def test_real_replies_with_our_gk_on_the_ball_are_rejected(self):
        for pid, text in ((1, DEF_REAL), (2, MID_REAL)):
            v = validate_reply(extract_reply(text), context_from_state(GK_HOLDS, 0, pid))
            self.assertFalse(v.ok)
            self.assertEqual([i.code for i in v.issues], ["own_ball"])

    def test_press_is_still_allowed_on_their_ball_a_loose_ball_and_unknown(self):
        cases = {"THEIRS": (state_with("agentId_3", ball=(-27.5, -6), moves=[(8, (-28, -6))]), 0),
                 "LOOSE": (LOOSE_IN_BOX, 0),
                 "UNKNOWN": (state_with("agentId_0"), 0),
                 "sample as away team (THEIRS)": (GAME_STATE, 1)}
        for name, (s, team) in cases.items():
            with self.subTest(name):
                self.assertTrue(validate_reply(extract_reply(DEF_REAL), context_from_state(s, team, 1)).ok)

    def test_handler_falls_back_and_the_fallback_does_not_press(self):
        for pid, cfg, text in ((1, DEF_CONFIG, DEF_REAL), (2, MID_CONFIG, MID_REAL)):
            app, agent = base.App(), base.Agent(reply=text)
            handler = agent_base.create_invoke_handler(app, agent, pid, "X", build_fallback(cfg), cfg)
            final = run(handler, GK_HOLDS, 0, pid)
            self.assertEqual(final, build_fallback(cfg)(GK_HOLDS, 0, pid))
            self.assertNotEqual(final[0]["commandType"], "PRESS_BALL")
            self.assertTrue(any("own_ball" in m for _, m in app.records))


class TestDurationPrompt(unittest.TestCase):

    def test_all_five_prompts_state_the_rule(self):
        for agent in ("ai-gk", "ai-def", "ai-mid", "ai-fwd1", "ai-fwd2"):
            with open(os.path.join(TEAM, agent, "src", "main.py"), encoding="utf-8") as f:
                self.assertIn('"duration" must be a whole number >= 0, never negative.', f.read(), agent)

    def test_negative_duration_is_still_rejected(self):
        text = '[{"commandType":"PRESS_BALL","playerId":4,"parameters":{"intensity":1.0},"duration":-1}]'
        v = validate_reply(extract_reply(text), context_from_state(GAME_STATE, 1, 4))
        self.assertEqual([i.code for i in v.issues], ["out_of_range"])


class TestGkRule(unittest.TestCase):

    def test_fires_for_a_loose_ball_in_our_box(self):
        cmds = gk_loose_ball_in_box(LOOSE_IN_BOX, 0, 0)
        self.assertEqual([c["commandType"] for c in cmds], ["INTERCEPT"])
        self.assertTrue(validate_reply(cmds, context_from_state(LOOSE_IN_BOX, 0, 0)).ok)

    def test_mirrored_for_the_away_team(self):
        s = state_with(None, ball=(42, -4))
        self.assertEqual(gk_loose_ball_in_box(s, 1, 0)[0]["commandType"], "INTERCEPT")
        self.assertIsNone(gk_loose_ball_in_box(s, 0, 0))  # that is the opponent's box for team 0

    def test_box_edges_are_the_documented_assumption(self):
        self.assertEqual((PENALTY_AREA_DEPTH, PENALTY_AREA_HALF_WIDTH), (16.5, 20.16))
        inside = [(-55, 0), (-55 + 16.5, 20.16), (-55 + 16.5, -20.16)]
        outside = [(-55 + 16.51, 0), (-45, 20.17), (-55.01, 0), (0, 0), (55, 0)]
        for x, y in inside:
            self.assertTrue(ball_in_own_penalty_area({"x": x, "y": y}, 0), (x, y))
        for x, y in outside:
            self.assertFalse(ball_in_own_penalty_area({"x": x, "y": y}, 0), (x, y))
        for bad in ({}, {"x": None, "y": 0}, {"x": "1", "y": 0}, {"x": True, "y": 0}, {"x": float("nan"), "y": 0}):
            self.assertFalse(ball_in_own_penalty_area(bad, 0), bad)

    def test_does_not_fire_otherwise(self):
        cases = {
            "not the GK": (LOOSE_IN_BOX, 0, 1),
            "our GK holds it": (GK_HOLDS, 0, 0),
            "opponent holds it in our box": (state_with("agentId_3", ball=(-42, 4), moves=[(8, (-42, 4))]), 0, 0),
            "ambiguous holder": (state_with("agentId_0"), 0, 0),
            "loose but outside the box": (state_with(None, ball=(-30, 0)), 0, 0),
            "sample state": (GAME_STATE, 0, 0),
            "empty state": ({}, 0, 0),
        }
        for name, (s, team, pid) in cases.items():
            with self.subTest(name):
                self.assertIsNone(gk_loose_ball_in_box(s, team, pid))

    def test_handler_answers_without_the_model(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG,
                                                   rule_fn=gk_loose_ball_in_box)
        final = run(handler, LOOSE_IN_BOX)
        self.assertEqual(final, gk_loose_ball_in_box(LOOSE_IN_BOX, 0, 0))
        self.assertEqual(agent.calls, [])  # the model was not called
        self.assertTrue(any("answered by rule gk_loose_ball_in_box" in m for _, m in app.records))

    def test_handler_still_asks_the_model_when_the_rule_does_not_fire(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG,
                                                   rule_fn=gk_loose_ball_in_box)
        self.assertEqual(run(handler, GAME_STATE)[0]["commandType"], "MOVE_TO")
        self.assertEqual(len(agent.calls), 1)

    def test_an_invalid_rule_output_goes_to_the_model(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        bad = lambda gs, t, p: [{"commandType": "INTERCEPT", "playerId": 3, "teamId": t, "parameters": {}}]
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG,
                                                   rule_fn=bad)
        self.assertEqual(run(handler, LOOSE_IN_BOX)[0]["commandType"], "MOVE_TO")
        self.assertEqual(len(agent.calls), 1)
        self.assertTrue(any("Rule <lambda> output failed validation" in m for _, m in app.records))

    def test_gk_main_wires_the_rule(self):
        with open(os.path.join(TEAM, "ai-gk", "src", "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("rule_fn=chain(gk_loose_ball_in_box, gk_distribute)", src)


class TestRealFwd1Errors(unittest.TestCase):
    """The three FWD1 (Nova Micro) rejections logged by the deployed runtime on 2026-10-18:
    FOLLOW_PLAYER with the followed player's id in playerId and the parameters at top level."""

    REPLIES = ['[ {"commandType":"FOLLOW_PLAYER","playerId":0,"target_team":"AWAY","distance":3.0} ]',
               '[{"commandType":"FOLLOW_PLAYER","playerId":1,"targetTeam":"AWAY","distance":2.5}]',
               '[{"commandType":"FOLLOW_PLAYER","playerId":1,"target_team":"AWAY","distance":1.0}]']

    def test_all_three_are_still_rejected_and_never_repaired(self):
        for text in self.REPLIES:
            v = validate_reply(extract_reply(text), context_from_state(GAME_STATE, 0, 3))
            self.assertFalse(v.ok)
            self.assertIn("wrong_player", [i.code for i in v.issues])

    def test_the_well_formed_command_is_accepted(self):
        good = ('[{"commandType":"FOLLOW_PLAYER","playerId":3,"parameters":'
                '{"target_player_id":1,"target_team":"AWAY","distance":3.0}}]')
        self.assertTrue(validate_reply(extract_reply(good), context_from_state(GAME_STATE, 0, 3)).ok)

    def test_all_five_prompts_explain_player_id_and_parameters(self):
        for pid, agent in enumerate(("ai-gk", "ai-def", "ai-mid", "ai-fwd1", "ai-fwd2")):
            with open(os.path.join(TEAM, agent, "src", "main.py"), encoding="utf-8") as f:
                src = f.read()
            self.assertIn('"playerId" is always {MY_PLAYER_ID} (you). Every parameter goes inside "parameters"; '
                          'the player you pass to, mark or follow is parameters.target_player_id.', src, agent)
            self.assertIn(f"MY_PLAYER_ID = {pid}", src, agent)


class TestInterceptOwnBall(unittest.TestCase):

    def test_rejected_when_we_hold_the_ball_allowed_otherwise(self):
        text = '[{"commandType":"INTERCEPT","playerId":1,"parameters":{"aggressive":true}}]'
        v = validate_reply(extract_reply(text), context_from_state(GK_HOLDS, 0, 1))
        self.assertEqual([i.code for i in v.issues], ["own_ball"])
        for s, team in ((LOOSE_IN_BOX, 0), (GAME_STATE, 1)):
            self.assertTrue(validate_reply(extract_reply(text), context_from_state(s, team, 1)).ok)


class TestProtocolWarning(unittest.TestCase):

    def handler(self):
        app, agent = base.App(), base.Agent(reply=base.CLEAN)
        return app, agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG)

    def collect(self, handler, payload):
        async def go():
            return [c async for c in handler(payload, None)]
        return json.loads(base.LOOP.run_until_complete(go())[0])

    def test_silent_for_the_expected_envelope(self):
        app, h = self.handler()
        self.collect(h, {"prompt": json.dumps({"gameState": GAME_STATE, "teamId": 0, "myPlayers": [0]})})
        self.assertFalse([m for _, m in app.records if agent_base.PROTOCOL_MARKER in m])

    def test_names_the_keys_but_never_the_values(self):
        cases = {"other envelope": {"state": {"secretValue": 123}},
                 "no myPlayers": {"prompt": json.dumps({"gameState": GAME_STATE, "teamId": 1})},
                 "empty state": {"prompt": json.dumps({"gameState": {}, "teamId": 0, "myPlayers": [0]})},
                 "state not an object": {"prompt": json.dumps({"gameState": [1, 2], "teamId": 0, "myPlayers": [0]})}}
        for name, payload in cases.items():
            with self.subTest(name):
                app, h = self.handler()
                out = self.collect(h, payload)
                self.assertEqual(len(out), 1)  # still answers with one command
                warns = [m for _, m in app.records if agent_base.PROTOCOL_MARKER in m]
                self.assertEqual(len(warns), 1, app.records)
                self.assertNotIn("123", warns[0])
        app, h = self.handler()
        self.collect(h, cases["other envelope"])
        self.assertIn("payload keys=['state']", [m for _, m in app.records if agent_base.PROTOCOL_MARKER in m][0])


class TestSimulatedMatches(unittest.TestCase):
    """afc_tools/sim_match.py: toy physics, but the real handler pipeline on every tick."""

    def test_short_matches_stay_valid_and_compatible_and_are_deterministic(self):
        sys.path.insert(0, os.path.join(base.ROOT, "afc_tools"))
        import sim_match
        from collections import Counter
        runs = []
        for _ in range(2):
            stats = {"inference": Counter(), "situations": Counter(), "sources": Counter(), "commands": Counter(),
                     "chasers": Counter(), "possession_truth": Counter(), "invalid": [], "incompatible": [],
                     "score_for": 0, "score_against": 0}
            handlers = sim_match.build_team()
            for seed in (11, 12):
                sim_match.play(seed, 60, handlers, base.LOOP, stats)
            runs.append(stats)
        self.assertEqual(runs[0]["invalid"], [])
        self.assertEqual(runs[0]["incompatible"], [])
        self.assertEqual(sum(runs[0]["sources"].values()), 2 * 60 * 5)
        self.assertEqual(runs[0], runs[1])  # same seeds -> same match


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
