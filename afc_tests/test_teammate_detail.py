"""lib/state.summarize_state(..., teammate_detail=True) (experiment, off by default) and its use in
afc_tools/model_eval.py --enriched. Offline: no network, AWS or model."""

import copy
import json
import os
import re
import sys
import unittest

import test_afc_changes as base  # sys.path, network guard, AgentCore stub

from state import summarize_state
from test_helpers import GAME_STATE

sys.path.insert(0, os.path.join(base.ROOT, "afc_tools"))
import model_eval as me  # noqa: E402

DETAIL = re.compile(r"( distOppGoal=\S+)? nearestOpp=\S+$")


def teammate_lines(summary):
    block = summary.split("Teammates:\n", 1)[1].split("\n\n", 1)[0]
    return block.splitlines()


class TestDefaultUnchanged(unittest.TestCase):

    def test_off_by_default_and_identical_to_explicit_false(self):
        for team in (0, 1):
            for pid, label in enumerate(["GK", "DEF", "MID", "FWD1", "FWD2"]):
                s = summarize_state(GAME_STATE, team, pid, label)
                self.assertEqual(s, summarize_state(GAME_STATE, team, pid, label, teammate_detail=False))
                self.assertNotIn("nearestOpp", s)

    def test_only_teammate_lines_change(self):
        off = summarize_state(GAME_STATE, 0, 3, "FWD1").splitlines()
        on = summarize_state(GAME_STATE, 0, 3, "FWD1", teammate_detail=True).splitlines()
        self.assertEqual(len(off), len(on))
        changed = [i for i, (a, b) in enumerate(zip(off, on)) if a != b]
        self.assertEqual([off[i].split(":")[0].strip() for i in changed], ["GK(id=0)", "P1(id=1)", "P2(id=2)", "P4(id=4)"])
        for i in changed:
            self.assertEqual(DETAIL.sub("", on[i]), off[i])


class TestValues(unittest.TestCase):

    def test_same_definitions_as_the_player_line(self):
        lines = teammate_lines(summarize_state(GAME_STATE, 0, 3, "FWD1", teammate_detail=True))
        p4 = next(l for l in lines if l.strip().startswith("P4(id=4)"))
        # P4 at (20, 15): |20 - 55| = 35; nearest opponent away #2 at (25, 10): sqrt(50) = 7.07
        self.assertIn("distOppGoal=35.0 nearestOpp=7.1", p4)
        gk = next(l for l in lines if l.strip().startswith("GK(id=0)"))
        self.assertIn("distOppGoal=105.0", gk)

    def test_away_team_measures_to_the_other_goal(self):
        lines = teammate_lines(summarize_state(GAME_STATE, 1, 3, "FWD1", teammate_detail=True))
        gk = next(l for l in lines if l.strip().startswith("GK(id=0)"))  # away GK at (50, 0), goal x = -55
        self.assertIn("distOppGoal=105.0", gk)

    def test_mid_keeps_its_single_distoppgoal(self):
        lines = teammate_lines(summarize_state(GAME_STATE, 0, 2, "MID", teammate_detail=True))
        for l in lines:
            self.assertEqual(l.count("distOppGoal="), 1, l)
            self.assertIn("nearestOpp=", l)


class TestEdgeCases(unittest.TestCase):

    def lines(self, gs, pid=3):
        return {l.strip().split(":")[0]: l for l in teammate_lines(summarize_state(gs, 0, pid, "FWD1", teammate_detail=True))}

    def test_missing_or_non_numeric_teammate_position_is_unknown_not_zero(self):
        for bad in (None, {}, {"x": 5}, {"x": "5", "y": 0}, {"x": True, "y": 0}, {"x": float("nan"), "y": 0}):
            with self.subTest(bad=bad):
                gs = copy.deepcopy(GAME_STATE)
                if bad is None:
                    del gs["players"][4]["position"]
                else:
                    gs["players"][4]["position"] = bad
                if bad is not None and bad.get("x") == "5":
                    # PRE-EXISTING (unchanged upstream line): the coordinate printing itself raises for a
                    # string x, with or without the option; the handler then falls back for that tick
                    with self.assertRaises(ValueError):
                        summarize_state(gs, 0, 3, "FWD1")
                    continue
                self.assertIn("distOppGoal=unknown nearestOpp=unknown", self.lines(gs)["P4(id=4)"])

    def test_pre_existing_crash_on_null_opponent_x_is_unchanged(self):
        gs = copy.deepcopy(GAME_STATE)
        gs["players"][7]["position"] = {"x": None, "y": 3}
        for detail in (False, True):  # same behaviour in both modes: not introduced, not hidden
            with self.assertRaises(TypeError):
                summarize_state(gs, 0, 3, "FWD1", teammate_detail=detail)

    def test_no_opponents(self):
        gs = copy.deepcopy(GAME_STATE)
        gs["players"] = [p for p in gs["players"] if p["teamCode"] == "home"]
        line = self.lines(gs)["P4(id=4)"]
        self.assertIn("distOppGoal=35.0 nearestOpp=unknown", line)

    def test_opponents_with_missing_positions_are_skipped_not_taken_as_0_0(self):
        gs = copy.deepcopy(GAME_STATE)
        for p in gs["players"]:
            if p["teamCode"] == "away" and p["agentId"] != "agentId_0":
                p["position"] = {}  # the unchanged opponent lines still print these as (0.0,0.0)
        line = self.lines(gs)["P4(id=4)"]  # only the away GK (50, 0) is real: sqrt(30^2 + 15^2)
        self.assertIn("nearestOpp=33.5", line)


class TestEnrichedEvaluation(unittest.TestCase):

    FILES = {"shoot25": os.path.join(base.ROOT, "afc_tools", "prompt_variants", "shoot_threshold_25.json")}

    def plan(self, pos):
        return me.build_plan(pos, ["lite"], ["shoot25", "shoot25_rich"], 4, scenario_set="shooting_control",
                             variant_files=self.FILES, enriched={"shoot25_rich": "shoot25"},
                             base_prompt=open(os.path.join(base.ROOT, "afc_tests", "fixtures", "prompts", f"{pos}_pre_20261018.txt"),
                                              encoding="utf-8", newline="").read())

    def test_56_calls_per_forward_112_total_same_prompt_different_summary(self):
        total = 0
        for pos in ("fwd1", "fwd2"):
            p = self.plan(pos)
            total += p["calls"]
            self.assertEqual(p["calls"], 56)
            self.assertEqual(p["prompts"]["shoot25_rich"], p["prompts"]["shoot25"])
            for name in p["summaries"]:
                plain, rich = p["summaryByVariant"]["shoot25"][name], p["summaryByVariant"]["shoot25_rich"][name]
                self.assertNotIn("nearestOpp", plain)
                self.assertIn("nearestOpp", rich)
                self.assertEqual("\n".join(DETAIL.sub("", l) for l in rich.splitlines()), plain)
        self.assertEqual(total, 112)

    def test_the_model_really_receives_the_enriched_summary(self):
        p = self.plan("fwd1")
        seen = []

        def fake(m, prompt, summary):
            seen.append(("nearestOpp" in summary, prompt == p["prompts"]["shoot25"]))
            return {"text": "[]", "wallS": 0.1, "inTok": 1, "outTok": 1}
        rows, _ = me.run(p, fake, me.Budget(p["calls"], 1.0))
        by_variant = {}
        for row, (rich, same_prompt) in zip(rows, seen):
            by_variant.setdefault(row["variant"], set()).add((rich, same_prompt))
        self.assertEqual(by_variant, {"shoot25": {(False, True)}, "shoot25_rich": {(True, True)}})

    def test_unknown_base_variant_is_refused(self):
        with self.assertRaises(ValueError):
            me.build_plan("fwd1", ["lite"], ["x"], 1, enriched={"x": "nope"})


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
