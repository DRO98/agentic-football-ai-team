"""Offline tests for the lab fixes ported into this copy's lib/ (no model, AWS or network).

  stamina      lib/stamina.py + state.py        precise value or "unknown" (lab Exp 5)
  possession   lib/possession.py + state/fallback  team-aware OURS/THEIRS/LOOSE/UNKNOWN (lab Exp 2a)
  validator    lib/action_validator.py + agent_base  reply validated as written (lab Exp 3)
  history      lib/agent_base.create_agent       SlidingWindowConversationManager(window_size=0) by default

Run from C:\\dev\\afc\\agentic-football-sample-agents:
    ..\\.venv\\Scripts\\python.exe -m unittest discover -s afc_tests -v
"""

import copy
import json
import math
import os
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub, App/Agent doubles, tick()

import agent_base
from action_validator import context_from_state, extract_reply, validate_reply
from fallback import DEF_CONFIG, FWD1_CONFIG, FWD2_CONFIG, GK_CONFIG, MID_CONFIG, build_fallback
from possession import CONTROL_RADIUS, MIN_SEPARATION, Possession, resolve_possession
from state import summarize_state
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.models.model import Model
from test_helpers import GAME_STATE

NOVA_REPLY = '[{"commandType":"GK_DISTRIBUTE","playerId":0,"parameters":{"target_player_id":1,"method":"THROW"},"duration":0}]'
CONFIGS = [GK_CONFIG, DEF_CONFIG, MID_CONFIG, FWD1_CONFIG, FWD2_CONFIG]
LABELS = ["GK", "DEF", "MID", "FWD1", "FWD2"]


def me_line(summary):
    return next(l for l in summary.splitlines() if l.startswith(">>> YOUR PLAYER"))


def state_with(holder=None, ball=(15.3, -5.2), moves=(), players=None, **ball_extra):
    s = copy.deepcopy(GAME_STATE)
    s["ball"]["possessionAgentId"] = holder
    s["ball"]["isFree"] = holder is None
    s["ball"]["position"] = {"x": ball[0], "y": ball[1], "z": 0}
    s["ball"].update(ball_extra)
    for index, (x, y) in moves:
        s["players"][index]["position"] = {"x": x, "y": y}
    if players is not None:
        s["players"] = players
    return s


class TestStamina(unittest.TestCase):

    def test_precise_values(self):
        for value, shown in [(0, "0"), (0.0, "0.0"), (0.25, "0.25"), (0.5, "0.5"), (0.95, "0.95"), (1, "1")]:
            s = copy.deepcopy(GAME_STATE)
            s["players"][0]["stamina"] = value
            self.assertIn(f" stam={shown} ", me_line(summarize_state(s, 0, 0, "GK")))

    def test_sample_no_longer_rounds_to_one(self):
        expected = ["0.95", "0.8", "0.7", "0.65", "0.85"]
        for pid, label in enumerate(LABELS):
            self.assertIn(f" stam={expected[pid]} ", me_line(summarize_state(GAME_STATE, 0, pid, label)))

    def test_missing_or_invalid_is_unknown_and_never_raises(self):
        for value in (None, "0.8", True, math.nan, math.inf, -0.1, 95, [0.5]):
            s = copy.deepcopy(GAME_STATE)
            s["players"][0]["stamina"] = value
            self.assertIn(" stam=unknown ", me_line(summarize_state(s, 0, 0, "GK")))
        s = copy.deepcopy(GAME_STATE)
        del s["players"][0]["stamina"]
        self.assertIn(" stam=unknown ", me_line(summarize_state(s, 0, 0, "GK")))


class TestPossession(unittest.TestCase):

    def test_sample_resolves_by_proximity_between_same_id_players(self):
        info = resolve_possession(GAME_STATE["ball"], GAME_STATE["players"], 0)
        self.assertEqual((info.state, info.holder_idx, info.source), (Possession.OURS, 3, "inferred_proximity"))
        self.assertIs(resolve_possession(GAME_STATE["ball"], GAME_STATE["players"], 1).state, Possession.THEIRS)

    def test_opponent_keeper_on_the_ball_is_not_our_keeper(self):
        s = state_with("agentId_0", ball=(49.5, 0))  # away GK at (50, 0), our GK at (-50, 0)
        self.assertIn("held by OPP player 0", summarize_state(s, 0, 0, "GK"))
        self.assertIn("hasBall=False", me_line(summarize_state(s, 0, 0, "GK")))
        self.assertNotEqual(build_fallback(GK_CONFIG)(s, 0, 0)[0]["commandType"], "GK_DISTRIBUTE")

    def test_list_order_does_not_matter(self):
        s = state_with("agentId_0", ball=(49.5, 0))
        flipped = copy.deepcopy(s)
        flipped["players"] = s["players"][5:] + s["players"][:5]
        self.assertEqual(summarize_state(s, 0, 0, "GK"), summarize_state(flipped, 0, 0, "GK"))
        self.assertEqual(build_fallback(GK_CONFIG)(s, 0, 0), build_fallback(GK_CONFIG)(flipped, 0, 0))

    def test_ambiguous_states_are_unknown_and_hold(self):
        cases = {
            "both #0 far from the ball (upstream test_local fixture)": state_with("agentId_0"),
            "both #4 within 1.5 m": state_with("agentId_4", ball=(-38, 2), moves=[(4, (-37, 1)), (9, (-38.5, 2.5))]),
            "unknown id": state_with("agentId_6", ball=(18, -3)),
            "malformed id": state_with("goalie"),
            "isFree true with a holder": state_with("agentId_3", ball=(14, -5), isFree=True),
        }
        for name, s in cases.items():
            with self.subTest(name):
                s["ball"]["isFree"] = s["ball"].get("isFree", False) if name.startswith("isFree") else False
                info = resolve_possession(s["ball"], s["players"], 0)
                self.assertIs(info.state, Possession.UNKNOWN, info)
                cmd = build_fallback(GK_CONFIG)(s, 0, 0)[0]
                self.assertEqual(cmd["commandType"], "MOVE_TO")  # never distribute, never chase
                self.assertIn("held by UNKNOWN", summarize_state(s, 0, 0, "GK"))

    def test_thresholds_are_the_documented_assumptions(self):
        self.assertEqual((CONTROL_RADIUS, MIN_SEPARATION), (3.0, 5.0))
        just_out = state_with("agentId_3", ball=(14 + CONTROL_RADIUS + 0.01, -5))
        self.assertIs(resolve_possession(just_out["ball"], just_out["players"], 0).state, Possession.UNKNOWN)

    def test_loose_ball_near_goal_gk_intercepts(self):
        s = state_with(None, ball=(-41, 4))
        self.assertEqual(build_fallback(GK_CONFIG)(s, 0, 0)[0]["commandType"], "INTERCEPT")
        self.assertIn("held by free", summarize_state(s, 0, 0, "GK"))

    def test_gk_prompt_explains_unknown_and_free(self):
        with open(os.path.join(base.ROOT, "ai-team-strands-balanced", "ai-gk", "src", "main.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("2. If ball is loose near your box (summary says 'held by free') → INTERCEPT", src)
        self.assertIn("3. If the holder is UNKNOWN or hasBall=unknown → never GK_DISTRIBUTE", src)
        self.assertIn("4. Otherwise → MOVE_TO to stay between ball and goal center", src)


class TestValidator(unittest.TestCase):

    def test_the_real_nova_micro_reply(self):
        ctx = context_from_state(GAME_STATE, 0, 0)
        v = validate_reply(extract_reply(NOVA_REPLY), ctx)
        self.assertFalse(v.ok)
        self.assertEqual([i.code for i in v.issues], ["needs_ball"])  # structure + parameters pass
        holding = state_with("agentId_0", ball=(-50, 0))
        self.assertTrue(validate_reply(extract_reply(NOVA_REPLY), context_from_state(holding, 0, 0)).ok)

    def test_handler_rejects_it_and_uses_the_validated_fallback(self):
        app, agent = base.App(), base.Agent(reply=NOVA_REPLY)
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG)
        final = base.LOOP.run_until_complete(self._collect(handler, GAME_STATE, 0))
        self.assertEqual(final, build_fallback(GK_CONFIG)(GAME_STATE, 0, 0))
        self.assertTrue(any("rejected by validator" in m and "needs_ball" in m for _, m in app.records))

    async def _collect(self, handler, state, team):
        chunks = [c async for c in handler({"prompt": json.dumps({"gameState": state, "teamId": team})}, None)]
        return json.loads(chunks[0])

    def test_rejections(self):
        ctx = context_from_state(GAME_STATE, 0, 0)
        cases = {
            "PUNT": '[{"commandType":"GK_DISTRIBUTE","parameters":{"target_player_id":1,"method":"PUNT"}}]',
            "off pitch": '[{"commandType":"MOVE_TO","parameters":{"target_x":-80,"target_y":0}}]',
            "no coordinates": '[{"commandType":"MOVE_TO"}]',
            "two commands": '[{"commandType":"RESET"},{"commandType":"RESET"}]',
            "wrong player": '[{"commandType":"SET_STANCE","playerId":4,"parameters":{"stance":2}}]',
            "prose": "I will stay on my line.",
            "empty": "[]",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertFalse(validate_reply(extract_reply(text), ctx).ok)

    def test_accepted_reply_is_unchanged(self):
        reply = '[{"commandType":"MOVE_TO","playerId":0,"parameters":{"target_x":-55,"target_y":35,"sprint":false},"duration":0}]'
        v = validate_reply(extract_reply(reply), context_from_state(GAME_STATE, 1, 0))
        self.assertTrue(v.ok)
        self.assertEqual(v.commands[0]["parameters"], {"target_x": -55, "target_y": 35, "sprint": False})
        self.assertEqual(v.commands[0]["teamId"], 1)

    def test_invalid_fallback_falls_through_to_last_resort(self):
        app, agent = base.App(), base.Agent(reply="no json")
        bad = lambda gs, t, p: [{"commandType": "GK_DISTRIBUTE", "playerId": p, "teamId": t,
                                 "parameters": {"target_player_id": 1, "method": "PUNT"}}]
        handler = agent_base.create_invoke_handler(app, agent, 0, "GK", bad, GK_CONFIG)
        final = base.LOOP.run_until_complete(self._collect(handler, GAME_STATE, 1))
        self.assertEqual(final, [{"commandType": "SET_STANCE", "playerId": 0, "parameters": {"stance": 2},
                                  "duration": 0, "teamId": 1}])


class TestFallback(unittest.TestCase):

    def test_every_position_fallback_passes_the_validator(self):
        states = [GAME_STATE, state_with("agentId_0", ball=(49.5, 0)), state_with(None, ball=(-41, 4)),
                  state_with("agentId_4"), state_with("agentId_0", ball=(-50, 0))]
        for s in states:
            for team in (0, 1):
                for pid, cfg in enumerate(CONFIGS):
                    with self.subTest(state=s["ball"], team=team, pid=pid):
                        cmds = build_fallback(cfg)(s, team, pid)
                        self.assertTrue(validate_reply(cmds, context_from_state(s, team, pid)).ok, cmds)


class FakeModel(Model):
    """Offline strands Model: records how many messages each call receives."""

    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {"model_id": "afc-fake-offline"}

    def structured_output(self, *a, **kw):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.sent.append(len(messages))
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": self.reply}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


class TestHistory(unittest.TestCase):

    def setUp(self):
        self.saved = os.environ.pop(agent_base.HISTORY_ENV, None)

    def tearDown(self):
        os.environ.pop(agent_base.HISTORY_ENV, None)
        if self.saved is not None:
            os.environ[agent_base.HISTORY_ENV] = self.saved

    def run_ticks(self, n=5):
        model = FakeModel(base.CLEAN)
        agent = agent_base.create_agent("system prompt", model=model)
        for i in range(n):
            agent(f"tick {i}")
        return agent, model

    def test_default_is_a_zero_window(self):
        self.assertEqual(agent_base.DEFAULT_HISTORY_WINDOW, 0)
        agent, model = self.run_ticks()
        self.assertIsInstance(agent.conversation_manager, SlidingWindowConversationManager)
        self.assertEqual(agent.conversation_manager.window_size, 0)
        self.assertEqual(model.sent, [1, 1, 1, 1, 1])  # only the current summary each tick
        self.assertEqual(agent.messages, [])            # nothing kept between ticks or matches

    def test_override_restores_a_bounded_window(self):
        os.environ[agent_base.HISTORY_ENV] = "4"
        agent, model = self.run_ticks(6)
        self.assertEqual(agent.conversation_manager.window_size, 4)
        self.assertEqual(model.sent, [1, 3, 5, 5, 5, 5])  # bounded: never grows past the window
        self.assertLessEqual(len(agent.messages), 4)

    def test_invalid_override_means_the_default(self):
        for raw in ("", "abc", "-1", "2.5", " "):
            os.environ[agent_base.HISTORY_ENV] = raw
            self.assertEqual(agent_base.history_window(), 0, raw)

    def test_upstream_default_would_accumulate(self):
        from strands import Agent
        model = FakeModel(base.CLEAN)
        agent = Agent(model=model, system_prompt="system prompt")  # upstream construction
        for i in range(5):
            agent(f"tick {i}")
        self.assertEqual(model.sent, [1, 3, 5, 7, 9])
        self.assertEqual(agent.conversation_manager.window_size, 40)


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"


if __name__ == "__main__":
    unittest.main()
