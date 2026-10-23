"""The forwards' configuration prepared for deployment (2026-10-18) is exactly what was evaluated.
Offline: imports the agents' main.py (no model call)."""

import os
import re
import sys
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub

sys.path.insert(0, os.path.join(base.ROOT, "afc_tools"))
import model_eval as me  # noqa: E402

TEAM = os.path.join(base.ROOT, "ai-team-strands-balanced")
FIX = os.path.join(base.ROOT, "afc_tests", "fixtures", "prompts")
VARIANTS = os.path.join(base.ROOT, "afc_tools", "prompt_variants")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8", newline="") as f:
        return f.read()


def source(agent):
    with open(os.path.join(TEAM, agent, "src", "main.py"), encoding="utf-8") as f:
        return f.read()


class TestPromptsAreTheEvaluatedOnes(unittest.TestCase):

    def test_fwd1_is_c_plus_ids_as_evaluated(self):
        # report model-eval-fwd1-20261018-204731.json, variant "shoot25ids"
        self.assertEqual(me.load_prompt("fwd1"), fixture("fwd1_evaluated_shoot25ids.txt"))

    def test_fwd2_is_c_as_evaluated(self):
        # reports model-eval-fwd2-20261018-204818 / -205239.json, variant "shoot25"
        self.assertEqual(me.load_prompt("fwd2"), fixture("fwd2_evaluated_shoot25.txt"))

    def test_evaluated_texts_are_the_variant_files_applied_to_the_previous_prompts(self):
        for pos, pre, vf, ev in (("fwd1", "fwd1_pre_20261018.txt", "shoot_threshold_25_ids.json", "fwd1_evaluated_shoot25ids.txt"),
                                 ("fwd2", "fwd2_pre_20261018.txt", "shoot_threshold_25.json", "fwd2_evaluated_shoot25.txt")):
            spec = me.load_variant_file(os.path.join(VARIANTS, vf))
            self.assertEqual(me.apply_variant(fixture(pre), spec), fixture(ev), pos)

    def test_other_agents_prompts_untouched_by_this_change(self):
        for agent in ("ai-gk", "ai-def", "ai-mid"):
            src = source(agent)
            self.assertNotIn("distOppGoal (in the YOUR PLAYER line) is 25 or less", src, agent)
            self.assertNotIn("## Player ids", src, agent)


class TestModels(unittest.TestCase):

    EXPECTED = {"ai-gk": "us.amazon.nova-micro-v1:0", "ai-def": "us.amazon.nova-lite-v1:0",
                "ai-mid": "us.amazon.nova-2-lite-v1:0", "ai-fwd1": "us.amazon.nova-lite-v1:0",
                "ai-fwd2": "us.amazon.nova-lite-v1:0"}

    def test_model_per_agent(self):
        for agent, model in self.EXPECTED.items():
            ids = re.findall(r'create_agent\(SYSTEM_PROMPT, model_id="([^"]+)"\)', source(agent))
            self.assertEqual(ids, [model], agent)


class TestOptionsStayOff(unittest.TestCase):

    def test_teammate_detail_is_not_wired_into_any_agent(self):
        for path in [os.path.join(base.ROOT, "lib", "agent_base.py")] + \
                    [os.path.join(TEAM, a, "src", "main.py") for a in ("ai-gk", "ai-def", "ai-mid", "ai-fwd1", "ai-fwd2")]:
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("teammate_detail", f.read(), path)

    def test_platform_version_stays_opt_in(self):
        with open(os.path.join(TEAM, "agentcore", "agentcore.json"), encoding="utf-8") as f:
            self.assertNotIn("platformVersion", f.read())
        with open(os.path.join(TEAM, "agentcore", "cdk", "lib", "cdk-stack.ts"), encoding="utf-8") as f:
            self.assertIn("process.env.AFC_PLATFORM_VERSION", f.read())


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
