"""Learning from real usage: `feedback` (pending queue) and `improve` (generations)."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from tests.fakes import FakeBackend
from tests.test_cli import run_cli
from tests.test_evolve import write_suite
from wikiskill.config import load_suite
from wikiskill.improve import (FEEDBACK, FOOTER_MARK, MERGED, finished, generations, merge_pending, pending,
                               prepare_generation)
from wikiskill.util import read_text
from wikiskill.workspace import Workspace


def set_scorer(suite_dir: Path, scorer: dict) -> None:
    cfg = json.loads(read_text(suite_dir / "suite.json"))
    cfg["scorer"] = scorer
    (suite_dir / "suite.json").write_text(json.dumps(cfg), encoding="utf-8")


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.suite = write_suite(self.root / "suite")  # exact scorer: answers required

    def feedback(self, *argv):
        return run_cli("feedback", self.suite, *argv)

    def reserved(self):
        path = self.root / "con.csv"
        try:
            path.write_text("x", encoding="utf-8")
        except OSError:  # Windows itself refuses the name: a missing file is rejected just the same
            pass
        return path

    def test_good_result_becomes_the_answer_and_files_are_copied(self):
        source = self.root / "jan.csv"
        source.write_text("a,b\n", encoding="utf-8")
        code, out = self.feedback("--verdict", "good", "--prompt", "1月の合計は？", "--output", "4090",
                                  "--file", source)
        self.assertEqual(code, 0, out)
        self.assertIn("1 feedback row(s)", out)
        (row,) = pending(self.suite)
        self.assertEqual((row["prompt"], row["answer"]), ("1月の合計は？", "4090"))
        self.assertEqual(row["files"], [f"fb/{row['id']}/jan.csv"])
        self.assertTrue((self.suite / "files" / row["files"][0]).is_file())
        self.assertIn('"1月の合計は？"', read_text(self.suite / FEEDBACK))  # stored readable (UTF-8)

    def test_bad_with_correction_uses_the_correction(self):
        code, out = self.feedback("--verdict", "bad", "--prompt", "q", "--output", "100", "--correct", "90",
                                  "--note", "税抜で")
        self.assertEqual(code, 0, out)
        (row,) = pending(self.suite)
        self.assertEqual(row["answer"], "90")
        self.assertIn("税抜で", row["rubric"])

    def test_bad_without_correction_needs_a_judge_scorer(self):
        code, out = self.feedback("--verdict", "bad", "--prompt", "q", "--output", "100")
        self.assertEqual(code, 2)
        self.assertIn("--correct", out)
        self.assertEqual(pending(self.suite), [])
        set_scorer(self.suite, {"type": "llm_judge", "rubric": "be right"})
        code, out = self.feedback("--verdict", "bad", "--prompt", "q", "--output", "100", "--note", "too high")
        self.assertEqual(code, 0, out)
        (row,) = pending(self.suite)
        self.assertNotIn("answer", row)
        self.assertIn("The rejected response was: 100", row["rubric"])
        self.assertIn("too high", row["rubric"])

    def test_regex_scorer_gets_a_literal_pattern(self):
        set_scorer(self.suite, {"type": "regex"})
        code, out = self.feedback("--verdict", "good", "--prompt", "q", "--output", "total (approx 5.0")
        self.assertEqual(code, 0, out)
        (row,) = pending(self.suite)
        self.assertEqual(re.findall(row["answer"], "it is total (approx 5.0)"), ["total (approx 5.0"])
        self.assertIsNone(re.search(row["answer"], "total (approx 5x0"))  # '.' is literal

    def test_invalid_input_is_rejected(self):
        for argv in (("--verdict", "good", "--prompt", "q"),  # good without the result
                     ("--verdict", "good", "--prompt", " ", "--output", "1"),
                     ("--verdict", "good", "--prompt", "q", "--output", "1", "--file", self.root / "missing.csv"),
                     ("--verdict", "good", "--prompt", "q", "--output", "1", "--file", self.reserved())):
            code, _ = self.feedback(*argv)
            self.assertEqual(code, 2, argv)
        self.assertEqual(pending(self.suite), [])

    def test_rows_are_valid_tasks(self):
        self.feedback("--verdict", "good", "--prompt", "q1", "--output", "1")
        self.feedback("--verdict", "bad", "--prompt", "q2", "--correct", "2")
        rows = pending(self.suite)
        self.assertEqual(len({r["id"] for r in rows}), 2)
        tasks = self.suite / "tasks.jsonl"
        with tasks.open("a", encoding="utf-8") as f:  # the same rows must load as tasks once given a split
            for r in rows:
                f.write(json.dumps({**r, "split": "train"}) + "\n")
        self.assertEqual(len(load_suite(self.suite).tasks), 8 + 2)


class InitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_suite_starts_from_an_example_with_the_users_files(self):
        inputs = self.root / "examples-from-user"
        (inputs / "2026-01").mkdir(parents=True)
        (inputs / "2026-01" / "sales.csv").write_text("a\n", encoding="utf-8")
        suite = self.root / "work" / "suite"
        code, out = run_cli("init", suite, "--like", "translation-ja-en", "--inputs", inputs)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(read_text(suite / "suite.json"))["scorer"]["type"], "llm_judge")
        self.assertTrue((suite / "files" / "2026-01" / "sales.csv").is_file())
        self.assertIn("input files (1): 2026-01/sales.csv", out)
        code, out = run_cli("init", suite, "--like", "sales-analysis")
        self.assertEqual(code, 2, out)  # never overwrites a suite
        self.assertIn("already exists", out)
        self.assertEqual(run_cli("init", self.root / "s2", "--like", "sales-analysis", "--inputs",
                                 self.root / "missing")[0], 2)


class ImproveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.suite = write_suite(self.root / "suite")
        self.ws = self.root / "ws"
        self.dest = self.root / "home" / "skills"
        patcher = mock.patch("wikiskill.cli.make_backend", lambda *a, **k: FakeBackend())
        patcher.start()
        self.addCleanup(patcher.stop)

    def improve(self, *argv):
        return run_cli("improve", self.suite, "--workspace", self.ws, "--export", self.dest, "--workers", 1, *argv)

    def add_feedback(self, n):
        for i in range(n):
            code, out = run_cli("feedback", self.suite, "--verdict", "good", "--prompt", f"real {i}",
                                "--output", "TOTAL")
            self.assertEqual(code, 0, out)

    def test_generations_carry_skills_and_merge_feedback(self):
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertEqual([p.name for p in generations(self.ws)], ["gen-001"])
        exported = read_text(self.dest / "report-total" / "SKILL.md")
        self.assertIn(FOOTER_MARK, exported)
        self.assertIn(str(self.suite.resolve()), exported)

        code, out = self.improve()  # finished and no feedback yet
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do: 0 feedback row(s) pending", out)
        self.add_feedback(2)
        self.assertIn("nothing to do: 2", self.improve()[1])  # below --min-new 3

        self.add_feedback(1)
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertIn("merged feedback: 3", out)
        gen2 = self.ws / "gen-002"
        self.assertTrue((gen2 / "skills" / "report-total" / "SKILL.md").is_file())  # seeded from gen-001
        self.assertTrue((gen2 / "wiki" / "skill-impact.md").is_file())
        tasks = [json.loads(line) for line in read_text(self.suite / "tasks.jsonl").splitlines()]
        self.assertEqual([t["split"] for t in tasks[8:]], ["train", "train", "val"])
        self.assertEqual(len(read_text(self.suite / "tasks.jsonl.bak").splitlines()), 8)
        self.assertEqual(pending(self.suite), [])
        self.assertEqual(len(read_text(self.suite / MERGED).splitlines()), 3)
        state = json.loads(read_text(gen2 / "state.json"))
        self.assertEqual(state["baseline"], 1.0)  # the carried-over skill already solves the new tasks

    def test_an_unfinished_generation_is_resumed_without_merging(self):
        ws = Workspace(self.ws / "gen-001")
        ws.init()
        ws.save_state({"iteration": 2, "r_best": 0.5})
        self.add_feedback(3)
        chosen, merged = prepare_generation(self.suite, self.suite / "tasks.jsonl", self.ws, 3, 3)
        self.assertEqual((chosen.root, merged), (ws.root, 0))
        self.assertEqual(len(pending(self.suite)), 3)
        self.assertTrue(finished({"iteration": 4, "r_best": 0.5}, 3))
        self.assertTrue(finished({"iteration": 2, "r_best": 1.0}, 3))
        self.assertFalse(finished({"iteration": 1, "r_best": None}, 3))

    def test_an_interrupted_preparation_is_redone(self):
        self.assertEqual(self.improve()[0], 0)  # gen-001 finished
        self.add_feedback(3)
        staging = Workspace(self.ws / "gen-next")  # crash after init, before seeding and merging
        staging.init()
        chosen, merged = prepare_generation(self.suite, self.suite / "tasks.jsonl", self.ws, 3, 3)
        self.assertEqual((chosen.root.name, merged), ("gen-002", 3))
        self.assertTrue((chosen.skills / "report-total" / "SKILL.md").is_file())
        self.assertFalse((self.ws / "gen-next").exists())
        self.assertEqual(self.improve()[0], 0)  # evolves gen-002 to the end
        # crash after merging, before the rename: the merged tasks still get their generation
        staging = Workspace(self.ws / "gen-next")
        staging.init()
        chosen, merged = prepare_generation(self.suite, self.suite / "tasks.jsonl", self.ws, 3, 3)
        self.assertEqual(chosen.root.name, "gen-003")
        self.assertEqual(merged, 0)

    def test_merge_skips_rows_already_in_the_tasks_file(self):
        self.add_feedback(1)
        (row,) = pending(self.suite)
        with (self.suite / "tasks.jsonl").open("a", encoding="utf-8") as f:  # merged before a crash
            f.write(json.dumps({**row, "split": "train"}) + "\n")
        self.assertEqual(merge_pending(self.suite, self.suite / "tasks.jsonl"), 0)
        self.assertEqual(len(load_suite(self.suite).tasks), 9)
        self.assertEqual(pending(self.suite), [])

    def test_export_never_overwrites_a_foreign_skill(self):
        foreign = self.dest / "report-total" / "SKILL.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("my own skill", encoding="utf-8")
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertIn("skip report-total", out)
        self.assertEqual(read_text(foreign), "my own skill")


if __name__ == "__main__":
    unittest.main()
