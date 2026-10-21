"""Tests for the opencode port: estimates, model diversity and reporting."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
BRACKET = os.path.join(SKILL, "bracket.py")
sys.path.insert(0, SKILL)

import bracket as B  # noqa: E402


def cli(*args):
    return subprocess.run([sys.executable, BRACKET] + [str(a) for a in args],
                          capture_output=True, text=True)


class ArenaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arena-opencode-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ok(self, *args):
        r = cli(*args)
        self.assertEqual(r.returncode, 0, "bracket.py %s failed:\n%s%s"
                         % (" ".join(map(str, args)), r.stdout, r.stderr))
        return r.stdout

    def init(self, agents=4, models=None, judges=None, approved=False):
        d = os.path.join(self.tmp, "run")
        args = ["init", "--agents", agents, "--seed", 9, "--task",
                "Write a haiku about salt.", "--dir", d]
        if models:
            args += ["--models", ",".join(models)]
        if judges:
            args += ["--judge-models", ",".join(judges)]
        if approved:
            args.append("--approved")
        self.ok(*args)
        return d

    def state(self, d):
        with open(os.path.join(d, "arena.json"), encoding="utf-8") as fh:
            return json.load(fh)


class Estimates(ArenaCase):
    def test_levels_are_ordered_and_full_costs_more_than_quick(self):
        quick = B.estimate_payload(16, 10, "task", "rubric")
        full = B.estimate_payload(100, 10, "task", "rubric")
        self.assertLess(quick["levels"]["low"]["total"], quick["levels"]["expected"]["total"])
        self.assertLess(quick["levels"]["expected"]["total"], quick["levels"]["high"]["total"])
        self.assertGreater(full["calls"], quick["calls"])
        self.assertGreater(full["levels"]["expected"]["total"],
                           quick["levels"]["expected"]["total"])

    def test_baseline_adds_one_call_and_a_final_phase(self):
        plain = B.estimate_payload(16, 10, "task", "rubric")
        based = B.estimate_payload(16, 10, "task", "rubric", "earlier answer")
        self.assertEqual(based["calls"], plain["calls"] + 1)
        self.assertEqual(based["waves"], plain["waves"] + 1)
        self.assertEqual(based["levels"]["expected"]["phases"][-1]["phase"], "final judge")

    def test_free_unknown_and_explicit_prices(self):
        free = B.estimate_payload(16, 10, "task", "rubric", models=["free/model"],
                                  price_in=0, price_out=0)
        self.assertEqual(free["models"][0]["costs"]["expected"], 0.0)
        unknown = B.estimate_payload(16, 10, "task", "rubric", models=["nope/nothing"])
        self.assertIsNone(unknown["models"][0]["prices"])
        self.assertIsNone(unknown["models"][0]["costs"])
        paid = B.estimate_payload(16, 10, "task", "rubric", models=["paid/model"],
                                  price_in=1, price_out=2)
        self.assertGreater(paid["models"][0]["costs"]["expected"], 0)

    def test_estimate_cli_json_and_unknown_model(self):
        out = self.ok("estimate", "--quick", "--task", "task", "--models", "nope/nothing", "--json")
        report = json.loads(out)
        self.assertEqual(report["agents"], 16)
        self.assertIsNone(report["models"][0]["costs"])


class Diversity(ArenaCase):
    def test_competitor_assignment_is_balanced_and_deterministic(self):
        aids = B.agent_ids(7)
        one = B.assign_competitor_models(aids, ["p/a", "p/b", "p/c"], 4)
        two = B.assign_competitor_models(aids, ["p/a", "p/b", "p/c"], 4)
        self.assertEqual(one, two)
        counts = {}
        for model in one.values():
            counts[model] = counts.get(model, 0) + 1
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_every_job_has_an_assigned_model(self):
        d = self.init(agents=4, models=["p/a", "p/b"], judges=["q/judge"])
        state = self.state(d)
        for phase in ("spawn", "attack", "defend", "judge"):
            jobs = B.phase_jobs(state, phase)
            if phase == "spawn":
                self.assertEqual(len(jobs), 4)
            self.assertTrue(all(job["model"] for job in jobs), phase)
        for job in B.phase_jobs(state, "spawn"):
            self.assertIn(job["model"], ("p/a", "p/b"))
        for job in B.phase_jobs(state, "judge"):
            self.assertEqual(job["model"], "q/judge")

    def test_prompts_never_name_a_model(self):
        d = self.init(agents=4, models=["provider/alpha-model", "provider/beta-model"],
                      judges=["judge/gamma-model"])
        state = self.state(d)
        for phase in ("spawn", "attack", "defend", "judge"):
            for job in B.write_prompts(state, phase):
                with open(job["prompt"], encoding="utf-8") as fh:
                    brief = fh.read()
                for model in ("alpha-model", "beta-model", "gamma-model"):
                    self.assertNotIn(model, brief, "%s leaked into %s" % (model, job["id"]))

    def test_prompts_json_and_approved_estimate(self):
        d = self.init(agents=4, models=["p/a", "p/b"], judges=["q/j"], approved=True)
        report = json.loads(self.ok("prompts", "spawn", "--json", "--dir", d))
        self.assertEqual(report["phase"], "spawn")
        self.assertEqual(len(report["jobs"]), 4)
        for job in report["jobs"]:
            self.assertTrue(job["model"])
            self.assertTrue(job["prompt"])
            self.assertTrue(job["outputs"])
        state = self.state(d)
        self.assertIn("estimate", state)
        self.assertEqual(state["estimate"]["agents"], 4)

    def test_report_and_fallback(self):
        d = self.init(agents=4, models=["p/a", "p/b"], judges=["q/j"], approved=True)
        self.ok("fallback", "a001.spawn", "p/c", "--dir", d)
        state = self.state(d)
        self.assertEqual(state["models"]["competitors"]["a001"], "p/c")
        self.assertTrue(state["models"]["degraded"])
        self.assertEqual(state["models"]["fallbacks"][0]["job"], "a001.spawn")
        report = json.loads(self.ok("report", "--json", "--dir", d))
        self.assertEqual(report["strategy"], "diverse")
        self.assertTrue(report["degraded"])
        self.assertIn("p/c", report["per_model"])
        self.assertIn("not cleanly isolated", report["warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
