"""Server pitch <-> design pitch (lib/frame.py) and the sprint limit (lib/rules.limit_sprint), 2026-10-22.
Offline: no network, AWS or model (the network guard in test_afc_changes blocks any attempt).

Fixture: afc_tests/fixtures/real_states_20261022.json = the 70 open-play/reset prompts (tick >= 100) the
match server sent our GK in the first real match (we were team 0, home). x stays within +-6.4, the
players' y and the ball's z are lateral, the ball's y is its height.
"""

import copy
import json
import math
import os
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles

import agent_base
from action_validator import context_from_state, validate_reply
from fallback import DEF_CONFIG, FWD1_CONFIG, FWD2_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback
from frame import SCALE, SERVER_HALF_LENGTH, SERVER_HALF_WIDTH, is_server_frame, to_design_frame, to_server_frame
from possession import resolve_possession
from rules import SPRINT_MIN_STAMINA, def_shape_on_our_ball, gk_loose_ball_in_box, limit_sprint
from test_afc_tactics import run
from test_helpers import GAME_STATE

with open(os.path.join(base.ROOT, "afc_tests", "fixtures", "real_states_20261022.json"), encoding="utf-8") as f:
    REAL = json.load(f)
STATES = [c["gameState"] for c in REAL]
CONFIGS = [(0, "GK", GK_CONFIG, gk_loose_ball_in_box), (1, "DEF", DEF_CONFIG, def_shape_on_our_ball),
           (2, "MID", MID_CONFIG, None), (3, "FWD1", FWD1_CONFIG, None), (4, "FWD2", FWD2_CONFIG, None)]


def nearest_holder_team(gs):
    """Ground truth for a held ball: the same-id candidate nearest the ball, using the ball's (x, z)."""
    h = gs["ball"].get("possessionAgentId")
    b = gs["ball"]["position"]
    cands = [p for p in gs["players"] if p["agentId"] == h]
    return min(cands, key=lambda p: math.hypot(p["position"]["x"] - b["x"], p["position"]["y"] - b["z"]))["teamCode"]


def move(x, y, sprint=False, pid=1):
    return [{"commandType": "MOVE_TO", "playerId": pid, "teamId": 0,
             "parameters": {"target_x": x, "target_y": y, "sprint": sprint}, "duration": 0}]


class TestDetection(unittest.TestCase):

    def test_every_real_state_is_the_server_frame(self):
        self.assertEqual(sum(is_server_frame(g) for g in STATES), len(STATES))

    def test_sample_and_design_states_are_left_alone(self):
        self.assertFalse(is_server_frame(GAME_STATE))
        gs, scale = to_design_frame(GAME_STATE)
        self.assertIs(gs, GAME_STATE)
        self.assertEqual(scale, 1.0)

    def test_the_servers_own_kick_off_in_design_units_is_left_alone(self):
        kick_off = copy.deepcopy(STATES[0])
        for p in kick_off["players"]:
            p["position"] = {"x": p["position"]["x"] * SCALE, "y": p["position"]["y"] * SCALE}
        kick_off["players"][0]["position"]["x"] = -50.0  # the KICK_OFF captures had GKs at +-50
        self.assertFalse(is_server_frame(kick_off))

    def test_broken_states_are_not_converted(self):
        for gs in ({}, {"players": []}, {"players": [{"position": {"x": None, "y": 0}}], "ball": {"position": {"x": 0}}}, None):
            self.assertFalse(is_server_frame(gs), gs)


class TestConversion(unittest.TestCase):

    def test_ball_lateral_comes_from_z_and_height_moves_to_z(self):
        g = STATES[10]
        d, scale = to_design_frame(g)
        b = g["ball"]["position"]
        self.assertAlmostEqual(scale, 55 / 6.4)
        self.assertEqual(d["ball"]["position"], {"x": b["x"] * SCALE, "y": b["z"] * SCALE, "z": b["y"]})
        self.assertAlmostEqual(d["players"][3]["position"]["x"], g["players"][3]["position"]["x"] * SCALE)
        self.assertAlmostEqual(d["players"][3]["position"]["y"], g["players"][3]["position"]["y"] * SCALE)

    def test_input_state_is_not_modified(self):
        g = copy.deepcopy(STATES[5])
        to_design_frame(g)
        self.assertEqual(g, STATES[5])

    def test_converted_states_fit_the_design_pitch(self):
        for g in STATES:
            d, _ = to_design_frame(g)
            for p in d["players"]:
                self.assertLessEqual(abs(p["position"]["x"]), 55.0 + 1e-6)
                self.assertLessEqual(abs(p["position"]["y"]), 35.0)

    def test_move_to_is_scaled_back_and_kept_on_the_server_pitch(self):
        out = to_server_frame(move(25, 15), SCALE)[0]["parameters"]
        self.assertEqual((out["target_x"], out["target_y"]), (round(25 / SCALE, 2), round(15 / SCALE, 2)))
        out = to_server_frame(move(-55, 35), SCALE)[0]["parameters"]
        self.assertEqual((out["target_x"], out["target_y"]), (-SERVER_HALF_LENGTH, round(SERVER_HALF_WIDTH, 2)))

    def test_distances_are_scaled_and_other_params_kept(self):
        cmds = [{"commandType": "FOLLOW_PLAYER", "playerId": 1, "teamId": 0,
                 "parameters": {"target_player_id": 3, "target_team": "AWAY", "distance": 8.6}, "duration": 3}]
        out = to_server_frame(cmds, SCALE)[0]["parameters"]
        self.assertEqual(out, {"target_player_id": 3, "target_team": "AWAY", "distance": round(8.6 / SCALE, 2)})
        press = [{"commandType": "PRESS_BALL", "playerId": 1, "teamId": 0, "parameters": {"intensity": 0.8}, "duration": 3}]
        self.assertEqual(to_server_frame(press, SCALE), press)

    def test_design_frame_replies_are_unchanged(self):
        cmds = move(25, 15)
        self.assertIs(to_server_frame(cmds, 1.0), cmds)


class TestPossessionOnRealStates(unittest.TestCase):

    def test_never_the_wrong_team_and_mostly_known(self):
        counts = {"right": 0, "wrong": 0, "unknown": 0}
        for g in STATES:
            if not g["ball"].get("possessionAgentId"):
                continue
            truth = "OURS" if nearest_holder_team(g) == "home" else "THEIRS"
            d, _ = to_design_frame(g)
            got = resolve_possession(d["ball"], d["players"], 0).state.name
            counts["unknown" if got == "UNKNOWN" else ("right" if got == truth else "wrong")] += 1
        self.assertEqual(counts["wrong"], 0, counts)
        self.assertGreaterEqual(counts["right"], 33, counts)  # 1 before the conversion

    def test_the_raw_state_was_nearly_always_unknown(self):
        unknown = sum(resolve_possession(g["ball"], g["players"], 0).state.name == "UNKNOWN" for g in STATES)
        self.assertGreaterEqual(unknown, 45)


class TestSprintLimit(unittest.TestCase):

    def state(self, stamina):
        s = copy.deepcopy(GAME_STATE)
        s["players"][1]["stamina"] = stamina
        return s

    def test_low_stamina_turns_sprint_off(self):
        out = limit_sprint(move(0, 0, sprint=True), self.state(SPRINT_MIN_STAMINA - 0.01), 0, 1)
        self.assertIs(out[0]["parameters"]["sprint"], False)

    def test_enough_or_unknown_stamina_keeps_it(self):
        for st in (SPRINT_MIN_STAMINA, 0.9, 100, None, "x"):
            out = limit_sprint(move(0, 0, sprint=True), self.state(st), 0, 1)
            self.assertIs(out[0]["parameters"]["sprint"], True, st)

    def test_input_is_not_modified(self):
        cmds = move(0, 0, sprint=True)
        limit_sprint(cmds, self.state(0.1), 0, 1)
        self.assertIs(cmds[0]["parameters"]["sprint"], True)


class TestHandlerOnRealStates(unittest.TestCase):

    def test_model_reply_in_design_units_reaches_the_server_in_its_units(self):
        g = next(x for x in STATES if x["ball"].get("possessionAgentId") is None)
        reply = '[{"commandType":"MOVE_TO","playerId":3,"parameters":{"target_x":25,"target_y":15,"sprint":true},"duration":0}]'
        app, agent = base.App(), base.Agent(reply=reply)
        handler = agent_base.create_invoke_handler(app, agent, 3, "FWD1", build_fallback(FWD1_CONFIG), FWD1_CONFIG)
        out = run(handler, g, 0, 3)[0]["parameters"]
        self.assertEqual((out["target_x"], out["target_y"]), (round(25 / SCALE, 2), round(15 / SCALE, 2)))
        me = next(p for p in g["players"] if p["teamCode"] == "home" and p["agentId"] == "agentId_3")
        self.assertEqual(out["sprint"], not me["stamina"] < SPRINT_MIN_STAMINA)
        self.assertIn("(server frame, scale 8.59)", " ".join(m for _, m in app.records))
        self.assertIn("Your goal at x=-55", agent.calls[0])  # the model sees the design pitch

    def test_all_five_agents_on_all_real_states_stay_valid_and_on_the_pitch(self):
        silent = base.Agent(reply="(no usable answer)")  # every tick runs on rule / fallback
        for pid, label, cfg, rule in CONFIGS:
            app = base.App()
            handler = agent_base.create_invoke_handler(app, silent, pid, label, build_fallback(cfg), cfg, rule_fn=rule)
            for g in STATES:
                with self.subTest(agent=label, tick=g["tick"]):
                    (cmd,) = run(handler, g, 0, pid)
                    d, _ = to_design_frame(g)
                    back = to_design_frame(g)[1]
                    design_cmd = copy.deepcopy(cmd)
                    if cmd["commandType"] == "MOVE_TO":
                        p = cmd["parameters"]
                        self.assertLessEqual(abs(p["target_x"]), SERVER_HALF_LENGTH)
                        self.assertLessEqual(abs(p["target_y"]), SERVER_HALF_WIDTH + 0.01)
                        design_cmd["parameters"].update(target_x=p["target_x"] * back, target_y=p["target_y"] * back)
                    self.assertTrue(validate_reply([design_cmd], context_from_state(d, 0, pid)).ok, cmd)
            self.assertFalse(any("last resort" in m or "agent error" in m for _, m in app.records), label)

    def test_def_rule_now_fires_on_our_real_ball(self):
        fired = sum(def_shape_on_our_ball(to_design_frame(g)[0], 0, 1) is not None for g in STATES)
        raw = sum(def_shape_on_our_ball(g, 0, 1) is not None for g in STATES)
        self.assertGreater(fired, raw)
        self.assertGreaterEqual(fired, 10)


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
