"""Offline tests for the two tournament changes in this copy's lib/ (no model, AWS or network).

  1. lib/state.py       safe gameTime (finite number as before, anything else "Time: unknown")
  2. lib/agent_base.py  optional capture line, only when AFC_CAPTURE_STATE is 1/true/yes/on

Run from C:\\dev\\afc\\agentic-football-sample-agents:
    ..\\.venv\\Scripts\\python.exe -m unittest discover -s afc_tests -v
"""

import asyncio
import copy
import json
import math
import os
import socket
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

BLOCKED = []
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_real_connect, _real_gai = socket.socket.connect, socket.getaddrinfo


def _connect(self, address):
    if (address[0] if isinstance(address, tuple) else address) not in _LOOPBACK:
        BLOCKED.append(address)
        raise ConnectionRefusedError("afc tests: network disabled")
    return _real_connect(self, address)


def _gai(host, *a, **k):
    if host not in _LOOPBACK and host is not None:
        BLOCKED.append(host)
        raise socket.gaierror("afc tests: network disabled")
    return _real_gai(host, *a, **k)


socket.socket.connect, socket.getaddrinfo = _connect, _gai

from test_helpers import GAME_STATE, mock_agentcore  # noqa: E402

mock_agentcore()

import agent_base  # noqa: E402
from fallback import GK_CONFIG, build_fallback  # noqa: E402
from state import summarize_state  # noqa: E402

MISSING = object()
LOOP = asyncio.new_event_loop()
CLEAN = '[{"commandType":"MOVE_TO","playerId":0,"parameters":{"target_x":-50,"target_y":0,"sprint":false},"duration":0}]'


class App:
    def __init__(self):
        self.records = []
        rec = self.records

        class Log:
            def info(self, m): rec.append(("info", m))
            def warn(self, m): rec.append(("warn", m))
            def error(self, m): rec.append(("error", m))

        self.logger = Log()

    def entrypoint(self, fn):
        return fn


class Agent:
    def __init__(self, reply=CLEAN):
        self.reply, self.calls = reply, []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.reply


def tick(state, team=0):
    app, agent = App(), Agent()
    handler = agent_base.create_invoke_handler(app, agent, 0, "GK", build_fallback(GK_CONFIG), GK_CONFIG)
    prompt = {"gameState": state, "teamId": team}

    async def collect():
        return [c async for c in handler({"prompt": json.dumps(prompt)}, None)]

    return [json.loads(c) for c in LOOP.run_until_complete(collect())], agent, app.records, prompt


def with_time(value):
    g = copy.deepcopy(GAME_STATE)
    if value is MISSING:
        del g["gameTime"]
    else:
        g["gameTime"] = value
    return g


class TestGameTime(unittest.TestCase):

    def test_numbers_render_as_upstream_did(self):
        for value in (120.5, 0, 59.5, 60.5, 5400, -3):
            self.assertTrue(summarize_state(with_time(value), 0, 0, "GK").startswith(f"Time: {value:.0f}s | "))

    def test_missing_null_string_boolean_malformed(self):
        for value in (MISSING, None, "120", "", True, False, math.nan, math.inf, [1], {"s": 1}):
            with self.subTest(value):
                self.assertTrue(summarize_state(with_time(value), 0, 0, "GK").startswith("Time: unknown | "))

    def test_the_model_is_called_for_a_bad_game_time(self):
        for value in (None, "120"):
            final, agent, logs, _ = tick(with_time(value))
            self.assertEqual(len(agent.calls), 1)
            self.assertTrue(any("LLM returned" in m for _, m in logs))


class TestCapture(unittest.TestCase):

    def setUp(self):
        self.saved = os.environ.pop(agent_base.CAPTURE_ENV, None)

    def tearDown(self):
        os.environ.pop(agent_base.CAPTURE_ENV, None)
        if self.saved is not None:
            os.environ[agent_base.CAPTURE_ENV] = self.saved

    def captures(self, logs):
        return [m for _, m in logs if m.startswith(agent_base.CAPTURE_MARKER)]

    def test_off_by_default(self):
        _, _, logs, _ = tick(GAME_STATE)
        self.assertEqual(self.captures(logs), [])
        for off in ("", "0", "false", "no", "off", "2"):
            os.environ[agent_base.CAPTURE_ENV] = off
            self.assertEqual(self.captures(tick(GAME_STATE)[2]), [], off)

    def test_on_logs_one_line_that_round_trips(self):
        for on in ("1", "true", "YES", " on "):
            os.environ[agent_base.CAPTURE_ENV] = on
            final, agent, logs, prompt = tick(GAME_STATE, team=1)
            lines = self.captures(logs)
            self.assertEqual(len(lines), 1, on)
            self.assertEqual(json.loads(lines[0][len(agent_base.CAPTURE_MARKER):]), prompt)
            self.assertEqual(len(agent.calls), 1)

    def test_captured_even_when_the_summary_would_fail(self):
        os.environ[agent_base.CAPTURE_ENV] = "1"
        broken = copy.deepcopy(GAME_STATE)
        broken["players"][0]["position"] = {"x": "bad", "y": 0}  # still breaks the summary (not fixed here)
        final, agent, logs, prompt = tick(broken)
        self.assertEqual(len(self.captures(logs)), 1)
        self.assertEqual(agent.calls, [])

    def test_decisions_identical_with_capture_on_or_off(self):
        os.environ[agent_base.CAPTURE_ENV] = "0"
        off = tick(GAME_STATE)[0]
        os.environ[agent_base.CAPTURE_ENV] = "1"
        self.assertEqual(tick(GAME_STATE)[0], off)


def tearDownModule():
    assert not BLOCKED, f"network attempts were blocked: {BLOCKED}"


if __name__ == "__main__":
    unittest.main()
