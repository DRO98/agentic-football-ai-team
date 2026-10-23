"""afc_tools/model_eval.py offline: plan, scoring, heuristics and budget guards with a fake caller.
No network, AWS or model (the network guard in test_afc_changes blocks any attempt)."""

import io
import json
import os
import sys
import unittest
import unittest.mock

import test_afc_changes as base  # sys.path, network guard, AgentCore stub

sys.path.insert(0, os.path.join(base.ROOT, "afc_tools"))
import model_eval as me  # noqa: E402

LINE = me.PROMPT_LINES_ADDED[0].format(pid=3)


def scen(plan, name):
    return next((t, d) for n, t, d in plan["scenarios"] if n == name)


class TestPlan(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.plan = me.build_plan("fwd1", ["micro", "lite"], ["current", "deployed"], 3, seed=1)

    def test_108_calls_9_scenarios_4_combos_each_equally_often(self):
        self.assertEqual(self.plan["calls"], 108)
        self.assertEqual(len(self.plan["scenarios"]), 9)
        counts = {}
        for _, name, _, _, m, v in self.plan["order"]:
            counts[(name, m, v)] = counts.get((name, m, v), 0) + 1
        self.assertEqual(set(counts.values()), {3})
        self.assertEqual(len(counts), 9 * 4)

    def test_variants_differ_only_by_the_added_line(self):
        cur, dep = self.plan["prompts"]["current"], self.plan["prompts"]["deployed"]
        self.assertIn(LINE, cur)
        self.assertNotIn(LINE, dep)
        self.assertEqual(cur.replace(LINE, ""), dep)

    def test_estimate_uses_the_verified_prices(self):
        self.assertEqual(me.PRICE["micro"], (0.035, 0.14))
        self.assertEqual(me.PRICE["lite"], (0.06, 0.24))
        self.assertLess(self.plan["estUSD"], 0.01)

    def test_same_seed_same_order(self):
        again = me.build_plan("fwd1", ["micro", "lite"], ["current", "deployed"], 3, seed=1)
        key = lambda p: [(r, n, m, v) for r, n, _, _, m, v in p["order"]]
        self.assertEqual(key(again), key(self.plan))


class TestScoring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.plan = me.build_plan("fwd1", ["micro"], ["current"], 1)

    def score(self, text, name):
        t, d = scen(self.plan, name)
        return me.score_reply(text, d["gameState"], t, 3, self.plan["cfg"])

    def test_real_follow_player_error_is_a_protocol_error_with_its_fallback(self):
        r = self.score('[ {"commandType":"FOLLOW_PLAYER","playerId":0,"target_team":"AWAY","distance":3.0} ]',
                       "loose_ball_our_box")
        self.assertTrue(r["parsed"])
        self.assertFalse(r["accepted"])
        self.assertTrue(r["protocolError"])
        self.assertFalse(r["possessionRejected"])
        self.assertIn("wrong_player", r["codes"])
        self.assertEqual(r["rawCommandType"], "FOLLOW_PLAYER")
        self.assertIn("fallbackCommand", r)

    def test_unparseable_reply(self):
        r = self.score("I would shoot.", "fwd1_on_ball_attack")
        self.assertEqual((r["parsed"], r["accepted"], r["codes"]), (False, False, ["no_json"]))

    def test_shot_from_41_units_is_a_long_shot(self):
        r = self.score('[{"commandType":"SHOOT","playerId":3,"parameters":{"aim_location":"CENTER","power":0.9}}]',
                       "fwd1_on_ball_attack")
        self.assertTrue(r["accepted"])
        self.assertAlmostEqual(r["tactical"]["shotDistance"], 41.3, places=1)
        self.assertTrue(r["tactical"]["long_shot"])

    def test_shot_in_range_is_not_a_long_shot(self):
        r = self.score('[{"commandType":"SHOOT","playerId":3,"parameters":{"aim_location":"TL","power":0.8}}]',
                       "fwd1_on_ball_in_range")
        self.assertTrue(r["accepted"])
        self.assertFalse(r["tactical"]["long_shot"])

    def test_support_run_heuristic_when_a_teammate_holds(self):
        fwd = '[{"commandType":"MOVE_TO","playerId":3,"parameters":{"target_x":35,"target_y":-5,"sprint":true}}]'
        back = '[{"commandType":"MOVE_TO","playerId":3,"parameters":{"target_x":0,"target_y":0,"sprint":false}}]'
        self.assertTrue(self.score(fwd, "mid_on_ball_midfield")["tactical"]["support_run"])
        self.assertFalse(self.score(back, "mid_on_ball_midfield")["tactical"]["support_run"])
        self.assertNotIn("support_run", self.score(fwd, "opponent_attack")["tactical"])  # not our ball

    def test_possession_rejection_is_not_a_protocol_error(self):
        r = self.score('[{"commandType":"PRESS_BALL","playerId":3,"parameters":{"intensity":0.8}}]',
                       "mid_on_ball_midfield")
        self.assertEqual(r["codes"], ["own_ball"])
        self.assertTrue(r["possessionRejected"])
        self.assertFalse(r["protocolError"])

    def test_mark_on_our_ball_is_flagged_as_heuristic_only(self):
        r = self.score('[{"commandType":"MARK","playerId":3,"parameters":{"target_player_id":1,"tightness":"TIGHT"}}]',
                       "mid_on_ball_midfield")
        self.assertTrue(r["accepted"])
        self.assertTrue(r["tactical"]["defend_own_ball"])


class TestRunAndBudget(unittest.TestCase):

    def setUp(self):
        self.plan = me.build_plan("fwd1", ["micro", "lite"], ["current", "deployed"], 1)

    def test_full_run_with_a_fake_model_counts_tokens_cost_and_writes_every_row(self):
        budget = me.Budget(self.plan["calls"], 1.0)

        def fake(m, prompt, summary):
            budget.http_requests += 1
            return {"text": '[{"commandType":"MOVE_TO","playerId":3,"parameters":{"target_x":20,"target_y":0}}]',
                    "wallS": 0.5, "bedrockMs": 400, "inTok": 700, "outTok": 40}
        sink = io.StringIO()
        rows, stopped = me.run(self.plan, fake, budget, sink)
        self.assertIsNone(stopped)
        self.assertEqual(len(rows), 36)
        self.assertEqual(len(sink.getvalue().splitlines()), 36)
        self.assertAlmostEqual(budget.usd, 18 * (0.035 * 700 + 0.14 * 40) / 1e6 + 18 * (0.06 * 700 + 0.24 * 40) / 1e6)
        s = me.summarize(rows, self.plan["combos"])
        self.assertEqual(set(s), {"micro/current", "micro/deployed", "lite/current", "lite/deployed"})
        self.assertEqual(s["micro/current"]["acceptedPct"], 100)
        json.dumps(s)  # serialisable

    def test_missing_usage_is_flagged_and_charged_at_the_estimate(self):
        budget = me.Budget(self.plan["calls"], 1.0)
        fake = lambda m, p, s: {"text": "[]", "wallS": 0.1, "bedrockMs": 400, "inTok": 0, "outTok": 0}
        rows, _ = me.run(self.plan, fake, budget)
        self.assertTrue(all(r["usageMissing"] for r in rows))
        r = next(r for r in rows if r["model"] == "lite")
        self.assertAlmostEqual(r["usd"], (0.06 * self.plan["estInputTokens"] + 0.24 * 60) / 1e6)
        self.assertEqual(me.summarize(rows, self.plan["combos"])["lite/current"]["usageMissing"], 9)

    def test_stops_after_three_errors_in_a_row(self):
        budget = me.Budget(self.plan["calls"], 1.0)

        def boom(m, prompt, summary):
            budget.http_requests += 1
            raise RuntimeError("ThrottlingException")
        rows, stopped = me.run(self.plan, boom, budget)
        self.assertEqual(len(rows), 3)
        self.assertIn("3 failed calls in a row", stopped)

    def test_stops_when_http_requests_exceed_the_plan(self):
        budget = me.Budget(self.plan["calls"], 1.0)

        def retrying(m, prompt, summary):
            budget.http_requests += 2  # a hidden retry would double the requests
            return {"text": "[]", "wallS": 0.1, "inTok": 1, "outTok": 1}
        rows, stopped = me.run(self.plan, retrying, budget)
        self.assertLess(len(rows), self.plan["calls"])
        self.assertIn("HTTP requests", stopped)

    def test_stops_when_the_cost_cap_is_reached(self):
        budget = me.Budget(self.plan["calls"], 0.0001)

        def pricey(m, prompt, summary):
            budget.http_requests += 1
            return {"text": "[]", "wallS": 0.1, "inTok": 100000, "outTok": 0}
        rows, stopped = me.run(self.plan, pricey, budget)
        self.assertIn("estimated cost", stopped)
        self.assertLessEqual(len(rows), 2)


FIXTURES = os.path.join(base.ROOT, "afc_tests", "fixtures", "prompts")


def pre_prompt(pos):
    """The forward prompts BEFORE the 2026-10-18 change, i.e. what the shooting/passing experiments used."""
    with open(os.path.join(FIXTURES, f"{pos}_pre_20261018.txt"), encoding="utf-8", newline="") as f:
        return f.read()


VARIANT_C = os.path.join(base.ROOT, "afc_tools", "prompt_variants", "shoot_threshold_25.json")
OLD_SHOT_LINE = "- SHOOT whenever you have the ball within shooting range (~25 units from goal)\n"


class TestVariantFiles(unittest.TestCase):

    def test_variant_c_applies_once_to_both_forwards_and_only_there(self):
        spec = me.load_variant_file(VARIANT_C)
        for pos, pid in (("fwd1", 3), ("fwd2", 4)):
            v = me.variants(pre_prompt(pos), pid, {"shoot25": VARIANT_C})
            self.assertIn(OLD_SHOT_LINE, v["current"])
            self.assertNotIn(OLD_SHOT_LINE, v["shoot25"])
            self.assertIn("distOppGoal (in the YOUR PLAYER line) is 25 or less", v["shoot25"])
            self.assertEqual(v["shoot25"].replace(spec["replace"][0]["new"], OLD_SHOT_LINE), v["current"])
            self.assertNotEqual(v["deployed"], v["current"])

    def test_a_missing_or_repeated_target_is_an_error(self):
        with self.assertRaises(ValueError):
            me.apply_variant("abc", {"replace": [{"old": "zzz", "new": "y"}]})
        with self.assertRaises(ValueError):
            me.apply_variant("a a", {"replace": [{"old": "a", "new": "y"}]})

    def test_unknown_variant_name_is_refused(self):
        with self.assertRaises(ValueError):
            me.build_plan("fwd1", ["micro"], ["shoot25"], 1, scenario_set="shooting")  # no file given


class TestShootingScenarios(unittest.TestCase):

    EXPECT_X = {"shoot_long_range": 41, "shoot_in_range": 22, "pass_better_teammate": 41,
                "under_pressure": 30, "advance_open_space": 35}

    def test_five_situations_for_each_forward_as_designed(self):
        for pos, pid, partner in (("fwd1", 3, 4), ("fwd2", 4, 3)):
            plan = me.build_plan(pos, ["micro"], ["current"], 1, scenario_set="shooting")
            self.assertEqual([n for n, _, _ in plan["scenarios"]], list(self.EXPECT_X))
            for name, team, data in plan["scenarios"]:
                with self.subTest(pos=pos, scenario=name):
                    t = me.tactical_view(data["gameState"], team, pid, None)
                    self.assertEqual((t["possession"], t["holder"], t["carrier"]), ("OURS", pid, True))
                    self.assertEqual(t["myDistOppGoalX"], self.EXPECT_X[name])
                    self.assertEqual(t["betterTeammates"], [partner] if name == "pass_better_teammate" else [])
                    self.assertEqual(t["pressure"] >= 2, name == "under_pressure")
                    self.assertIn(f"distOppGoal={self.EXPECT_X[name]:.1f}", plan["summaries"][name])

    def test_fwd2_situations_mirror_fwd1(self):
        p1 = me.build_plan("fwd1", ["micro"], ["current"], 1, scenario_set="shooting")["scenarios"]
        p2 = me.build_plan("fwd2", ["micro"], ["current"], 1, scenario_set="shooting")["scenarios"]
        for (n, _, a), (_, _, b) in zip(p1, p2):
            pa = {(p["teamCode"], p["agentId"]): p["position"] for p in a["gameState"]["players"]}
            pb = {(p["teamCode"], p["agentId"]): p["position"] for p in b["gameState"]["players"]}
            swap = {"agentId_3": "agentId_4", "agentId_4": "agentId_3"}
            for (team, aid), pos in pa.items():
                mirrored = pb[(team, swap.get(aid, aid))]
                self.assertEqual((mirrored["x"], mirrored["y"]), (pos["x"], -pos["y"]), (n, team, aid))


class TestShootingExperimentPlan(unittest.TestCase):

    def test_60_calls_per_forward_120_in_total(self):
        total = 0
        for pos, model in (("fwd1", "micro"), ("fwd2", "lite")):
            plan = me.build_plan(pos, [model], ["deployed", "current", "shoot25"], 4, seed=1,
                                 scenario_set="shooting", variant_files={"shoot25": VARIANT_C}, base_prompt=pre_prompt(pos))
            self.assertEqual(plan["calls"], 60)
            self.assertLess(plan["estUSD"], 0.01)
            total += plan["calls"]
        self.assertEqual(total, 120)

    def test_max_calls_refuses_before_any_call(self):
        argv = sys.argv
        sys.argv = ["model_eval.py", "--position", "fwd1", "--models", "micro", "--variants", "deployed,current,shoot25",
                    "--variant-file", f"shoot25={VARIANT_C}", "--scenario-set", "shooting", "--reps", "4",
                    "--max-calls", "59", "--invoke", "--base-prompt", os.path.join(FIXTURES, "fwd1_pre_20261018.txt")]
        try:
            with unittest.mock.patch.object(me, "make_bedrock_caller", side_effect=AssertionError("no caller")):
                self.assertEqual(me.main(), 3)
        finally:
            sys.argv = argv

    def test_carrier_metrics(self):
        plan = me.build_plan("fwd1", ["micro"], ["current"], 1, scenario_set="shooting")
        replies = {"shoot_long_range": '[{"commandType":"SHOOT","playerId":3,"parameters":{"aim_location":"TL","power":0.9}}]',
                   "shoot_in_range": '[{"commandType":"SHOOT","playerId":3,"parameters":{"aim_location":"TL","power":0.9}}]',
                   "pass_better_teammate": '[{"commandType":"PASS","playerId":3,"parameters":{"target_player_id":4,"type":"THROUGH"}}]',
                   "under_pressure": '[{"commandType":"PASS","playerId":3,"parameters":{"target_player_id":2,"type":"GROUND"}}]',
                   "advance_open_space": '[{"commandType":"MOVE_TO","playerId":3,"parameters":{"target_x":32,"target_y":-2,"sprint":true}}]'}
        budget = me.Budget(plan["calls"], 1.0)
        fake = lambda m, p, s: {"text": replies[next(n for n, ss in plan["summaries"].items() if ss == s)],
                                "wallS": 0.1, "inTok": 1, "outTok": 1}
        rows, _ = me.run(plan, fake, budget)
        s = me.summarize(rows, plan["combos"])["micro/current"]
        self.assertEqual((s["shotsOverThreshold"], s["shotsWithinThreshold"]), (1, 1))
        self.assertEqual((s["farAccepted"], s["farShootPct"], s["nearShootPct"]), (4, 25.0, 100.0))
        self.assertEqual((s["passToBetter"], s["betterTeammateCases"], s["carrierPasses"], s["carrierAdvances"]), (1, 1, 2, 1))


VARIANT_C2 = os.path.join(base.ROOT, "afc_tools", "prompt_variants", "shoot_threshold_25_pass.json")


class TestPassingExperiment(unittest.TestCase):

    def test_c2_keeps_c_shooting_rule_and_adds_ordered_passing(self):
        c = me.load_variant_file(VARIANT_C)["replace"][0]["new"]
        c2 = me.load_variant_file(VARIANT_C2)["replace"][0]["new"]
        self.assertEqual(c.splitlines()[0], c2.splitlines()[0])  # same "SHOOT only when ... 25 or less" line
        for pos, pid in (("fwd1", 3), ("fwd2", 4)):
            v = me.variants(pre_prompt(pos), pid, {"shoot25": VARIANT_C, "shoot25pass": VARIANT_C2})
            self.assertNotIn(OLD_SHOT_LINE, v["shoot25pass"])
            for text in ("no opponent position is within about 5 units of him", "his distToMe is under 3",
                         "Do not pass back to the GK (id 0) or to P1 (id 1, the defender)", "MOVE_TO toward the opponent goal"):
                self.assertIn(text, v["shoot25pass"])

    def test_seven_scenarios_and_140_calls(self):
        files = {"shoot25": VARIANT_C, "shoot25pass": VARIANT_C2}
        p1 = me.build_plan("fwd1", ["lite"], ["current", "shoot25", "shoot25pass"], 4, scenario_set="shooting_control", variant_files=files, base_prompt=pre_prompt("fwd1"))
        p2 = me.build_plan("fwd2", ["lite"], ["shoot25", "shoot25pass"], 4, scenario_set="shooting_control", variant_files=files, base_prompt=pre_prompt("fwd2"))
        names = [n for n, _, _ in p1["scenarios"]]
        self.assertEqual(names, list(TestShootingScenarios.EXPECT_X) + list(me.CONTROL_SCENARIOS))
        self.assertEqual((p1["calls"], p2["calls"], p1["calls"] + p2["calls"]), (84, 56, 140))
        for plan, pid in ((p1, 3), (p2, 4)):  # the controls really are off-ball for each forward
            for n, t, d in plan["scenarios"][5:]:
                self.assertNotIn("carrier", me.tactical_view(d["gameState"], t, pid, None))

    def test_new_pass_and_advance_metrics(self):
        plan = me.build_plan("fwd2", ["lite"], ["current"], 1, scenario_set="shooting_control")
        replies = {"under_pressure": '[{"commandType":"PASS","playerId":4,"parameters":{"target_player_id":1,"type":"GROUND"}}]',
                   "pass_better_teammate": '[{"commandType":"PASS","playerId":4,"parameters":{"target_player_id":3,"type":"THROUGH"}}]',
                   "advance_open_space": '[{"commandType":"MOVE_TO","playerId":4,"parameters":{"target_x":32,"target_y":2,"sprint":true}}]',
                   "shoot_long_range": '[{"commandType":"MOVE_TO","playerId":4,"parameters":{"target_x":14.5,"target_y":5}}]'}
        fallback_text = '[{"commandType":"SHOOT","playerId":4,"parameters":{"aim_location":"TL","power":0.9}}]'
        name_of = {s: n for n, s in plan["summaries"].items()}
        fake = lambda m, p, s: {"text": replies.get(name_of[s], fallback_text), "wallS": 0.1, "inTok": 1, "outTok": 1}
        rows, _ = me.run(plan, fake, me.Budget(plan["calls"], 1.0))
        s = me.summarize(rows, plan["combos"])["lite/current"]
        self.assertEqual((s["pressureCases"], s["backwardPassesUnderPressure"], s["forwardOrLevelPassesUnderPressure"]), (1, 1, 0))
        self.assertEqual((s["backwardPasses"], s["passesToBackline"], s["passToBetter"]), (1, 1, 1))
        self.assertEqual((s["carrierAdvances"], s["carrierAdvancesClear"], s["carrierStandStill"]), (1, 1, 1))
        self.assertEqual(s["offBallReplies"], 2)


VARIANT_IDS = os.path.join(base.ROOT, "afc_tools", "prompt_variants", "shoot_threshold_25_ids.json")


class TestIdMappingExperiment(unittest.TestCase):

    def test_differs_from_c_only_by_the_id_section(self):
        ids_section = me.load_variant_file(VARIANT_IDS)["replace"][1]["new"].replace(
            "## Available Commands (commandType → parameters)\n", "")
        for pos, pid in (("fwd1", 3), ("fwd2", 4)):
            v = me.variants(pre_prompt(pos), pid, {"shoot25": VARIANT_C, "shoot25ids": VARIANT_IDS})
            self.assertIn("P1(id=1) = DEF", v["shoot25ids"])
            self.assertIn("3 = Forward 1, 4 = Forward 2", v["shoot25ids"])
            self.assertEqual(v["shoot25ids"].replace(ids_section, ""), v["shoot25"])
            self.assertEqual(v["shoot25ids"].count("## Player ids"), 1)

    def test_112_calls_and_pass_target_breakdown(self):
        files = {"shoot25": VARIANT_C, "shoot25ids": VARIANT_IDS}
        plans = [me.build_plan(p, ["lite"], ["shoot25", "shoot25ids"], 4, scenario_set="shooting_control", variant_files=files, base_prompt=pre_prompt(p))
                 for p in ("fwd1", "fwd2")]
        self.assertEqual([p["calls"] for p in plans], [56, 56])
        p = plans[1]
        name_of = {s: n for n, s in p["summaries"].items()}
        text = {"under_pressure": '[{"commandType":"PASS","playerId":4,"parameters":{"target_player_id":3,"type":"GROUND"}}]'}
        fake = lambda m, pr, s: {"text": text.get(name_of[s], "[]"), "wallS": 0.1, "inTok": 1, "outTok": 1}
        rows, _ = me.run(p, fake, me.Budget(p["calls"], 1.0))
        s = me.summarize(rows, p["combos"])["lite/shoot25ids"]
        self.assertEqual(s["passTargets"], {"3": 4})
        self.assertEqual(s["underPressureChoices"], {"PASS>3": 4})


VARIANT_FWD2P = os.path.join(base.ROOT, "afc_tools", "prompt_variants", "shoot_threshold_25_ids_fwd2_pressure.json")


class TestFwd2PressureVariant(unittest.TestCase):

    def setUp(self):
        self.v = me.variants(pre_prompt("fwd2"), 4, {"shoot25": VARIANT_C, "fwd2p": VARIANT_FWD2P})["fwd2p"]

    def test_applies_only_to_fwd2_and_replaces_every_conflicting_line(self):
        self.assertNotIn(OLD_SHOT_LINE, self.v)
        self.assertNotIn("- PASS to Forward 1 or Midfielder if you're under pressure\n", self.v)
        self.assertNotIn("PASS to the nearest free teammate", self.v)  # C's clause, superseded by the pressure rule
        self.assertIn("Forward 1 (id 3) or the MID (id 2)", self.v)
        self.assertIn("3 = Forward 1", self.v)
        with self.assertRaises(ValueError):  # FWD1 has no such pressure line: the variant cannot apply there
            me.variants(pre_prompt("fwd1"), 3, {"fwd2p": VARIANT_FWD2P})

    def test_does_not_contradict_the_output_protocol(self):
        self.assertIn('"playerId" is always 4 (you)', self.v)          # unchanged protocol line
        self.assertNotRegex(self.v, r"playerId[\" ]*(is|=)?\s*[0-3]\b")  # never tells it to use another playerId
        self.assertIn("parameters.target_player_id", self.v)

    def test_plan_is_56_calls(self):
        p = me.build_plan("fwd2", ["lite"], ["shoot25", "fwd2p"], 4, scenario_set="shooting_control",
                          variant_files={"shoot25": VARIANT_C, "fwd2p": VARIANT_FWD2P}, base_prompt=pre_prompt("fwd2"))
        self.assertEqual(p["calls"], 56)


class TestCallerHasNoHiddenRetries(unittest.TestCase):

    def test_botocore_single_attempt_and_request_counter(self):
        budget = me.Budget(1, 1.0)
        call = me.make_bedrock_caller("us-east-1", None, ["micro"], budget)  # builds clients, calls nothing
        models = [c.cell_contents for c in call.__closure__ if isinstance(c.cell_contents, dict)][0]
        client = models["micro"].client
        self.assertEqual(client.meta.config.retries, {"total_max_attempts": 1, "mode": "standard"})
        client.meta.events.emit("before-send.bedrock-runtime.ConverseStream", request=None)
        self.assertEqual(budget.http_requests, 1)


def tearDownModule():
    assert not base.BLOCKED, f"network attempts were blocked: {base.BLOCKED}"
