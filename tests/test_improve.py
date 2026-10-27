"""Learning from real usage: `feedback` (pending queue) and `improve` (rounds in one workspace)."""
import contextlib
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeBackend, TempDirTest
from tests.test_cli import run_cli
from tests.test_evolve import write_suite
from wikiskill.config import load_suite
from wikiskill.evolve import has_work, is_done
from wikiskill.improve import FEEDBACK, MERGED, merge_pending, merge_ready, pending
from wikiskill.skills import EXPORT_MARK
from wikiskill.util import read_text
from wikiskill.workspace import Workspace

FEEDBACK_HEADING = "## Feedback for automatic improvement"


def set_scorer(suite_dir: Path, scorer: dict) -> None:
    cfg = json.loads(read_text(suite_dir / "suite.json"))
    cfg["scorer"] = scorer
    (suite_dir / "suite.json").write_text(json.dumps(cfg), encoding="utf-8")


class FeedbackTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.suite = write_suite(self.root / "suite")  # exact scorer: answers required

    def feedback(self, *argv):
        return run_cli("feedback", self.suite, *argv)

    def reserved(self):
        path = self.root / "con.csv"
        with contextlib.suppress(OSError):  # Windows refuses the name: a missing file is rejected just the same
            path.write_text("x", encoding="utf-8")
        return path

    def test_good_result_becomes_the_answer_and_files_are_copied(self):
        source = self.root / "jan.csv"
        source.write_text("a,b\n", encoding="utf-8")
        code, out = self.feedback("--verdict", "good", "--prompt", "1月の合計は？", "--output", "4090",
                                  "--file", source)
        self.assertEqual(code, 0, out)
        self.assertIn("1 feedback row(s)", out)
        self.assertNotIn("ready", out)
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


class InitTest(TempDirTest):
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


class ImproveTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.suite = write_suite(self.root / "suite")
        self.ws = self.root / "ws"
        self.dest = self.root / "home" / "skills"
        patcher = mock.patch("wikiskill.cli.make_backend", lambda *a, **k: FakeBackend())
        patcher.start()
        self.addCleanup(patcher.stop)

    def improve(self, *argv):
        return run_cli("improve", self.suite, "--workspace", self.ws, "--export", self.dest, "--workers", 1, *argv)

    def add_feedback(self, n, output="TOTAL"):
        for i in range(n):
            code, out = run_cli("feedback", self.suite, "--verdict", "good", "--prompt", f"real {i} {output}",
                                "--output", output)
            self.assertEqual(code, 0, out)

    def state(self):
        return Workspace(self.ws).load_state()

    def test_feedback_starts_a_new_round_in_the_same_workspace(self):
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        first = self.state()
        exported = read_text(self.dest / "report-total" / "SKILL.md")
        self.assertIn(FEEDBACK_HEADING, exported)
        self.assertIn(str(self.suite.resolve()), exported)

        code, out = self.improve()  # finished and no feedback yet
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do: 0 feedback row(s) pending", out)
        self.add_feedback(2)
        self.assertIn("nothing to do: 2", self.improve()[1])  # below --min-new 3

        code, out = run_cli("feedback", self.suite, "--verdict", "good", "--prompt", "real 9", "--output", "TOTAL")
        self.assertIn("ready: run `improve`", out)  # the third row tells the caller to run improve
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertIn("merged feedback: 3", out)
        self.assertEqual(list(self.ws.glob("gen-*")), [])  # no copies: the same workspace goes on
        tasks = [json.loads(line) for line in read_text(self.suite / "tasks.jsonl").splitlines()]
        self.assertEqual([t["split"] for t in tasks[8:]], ["train", "train", "val"])
        self.assertEqual(len(read_text(self.suite / "tasks.jsonl.bak").splitlines()), 8)
        self.assertEqual(pending(self.suite), [])
        self.assertEqual(len(read_text(self.suite / MERGED).splitlines()), 3)
        state = self.state()
        self.assertEqual(state["baseline"], first["baseline"])  # still the first, skill-less score
        self.assertEqual(state["round_start"], first["iteration"])
        self.assertEqual(state["history"][:len(first["history"])], first["history"])
        self.assertEqual(state["history"][-1]["outcome"], "new_tasks")
        self.assertEqual(state["r_best"], 1.0)  # the kept skill already solves the new tasks
        self.assertIn(f"## Round 2: tasks updated before iteration {first['iteration']}",
                      read_text(self.ws / "wiki" / "skill-impact.md"))

    def test_a_new_round_runs_more_iterations_with_continued_numbers(self):
        self.assertEqual(self.improve()[0], 0)
        start = self.state()["iteration"]
        self.add_feedback(3, output="OTHER")  # results the skill does not produce: validation drops below 1.0
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        state = self.state()
        rows = [h for h in state["history"] if h["iteration"] >= start]
        self.assertEqual(rows[0]["outcome"], "new_tasks")
        self.assertEqual([h["iteration"] for h in rows[1:]], [start, start + 1, start + 2])  # --iterations 3
        self.assertEqual(state["iteration"], start + 3)
        self.assertTrue((self.ws / "raw" / f"iter_{start + 2:03d}").is_dir())
        self.assertIn("nothing to do", self.improve()[1])

    def test_an_unfinished_round_is_resumed_without_merging(self):
        ws = Workspace(self.ws)
        ws.init()
        ws.save_state({"iteration": 2, "r_best": 0.5})
        self.add_feedback(3)
        self.assertEqual(merge_ready(self.suite, self.suite / "tasks.jsonl", ws.load_state(), 3, 3), 0)
        self.assertEqual(len(pending(self.suite)), 3)
        self.assertTrue(has_work(ws.load_state(), load_suite(self.suite), 3))

    def test_a_round_is_done_after_its_iterations_or_a_perfect_score(self):
        self.assertTrue(is_done({"iteration": 4, "r_best": 0.5}, 3))
        self.assertTrue(is_done({"iteration": 2, "r_best": 1.0}, 3))
        self.assertFalse(is_done({"iteration": 1, "r_best": None}, 3))
        self.assertFalse(is_done({"iteration": 5, "round_start": 3, "r_best": 0.5}, 3))
        self.assertTrue(is_done({"iteration": 6, "round_start": 3, "r_best": 0.5}, 3))

    def test_a_crash_after_the_merge_still_starts_the_round(self):
        self.assertEqual(self.improve()[0], 0)
        self.add_feedback(3)
        self.assertEqual(merge_pending(self.suite, self.suite / "tasks.jsonl"), 3)  # then the process died
        self.assertTrue(has_work(self.state(), load_suite(self.suite), 3))
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertIn("merged feedback: 0", out)
        self.assertEqual(self.state()["history"][-1]["outcome"], "new_tasks")

    def test_a_workspace_of_generations_continues_in_its_newest_one(self):
        legacy = self.ws / "gen-002"  # layout written by 0.2.x
        code, out = run_cli("improve", self.suite, "--workspace", legacy, "--workers", 1)
        self.assertEqual(code, 0, out)
        (self.ws / "gen-001").mkdir()
        code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do", out)
        self.assertFalse((self.ws / "state.json").exists())
        self.assertTrue((self.dest / "report-total" / "SKILL.md").is_file())  # exported from gen-002
        code, out = run_cli("status", self.ws)
        self.assertEqual(code, 0, out)
        self.assertIn("report-total", out)
        self.assertIn("exported report-total", run_cli("export", self.ws, "--dest", self.root / "other")[1])
        code, out = run_cli("evolve", self.suite, "--workspace", self.ws, "--iterations", 3, "--workers", 1)
        self.assertEqual(code, 0, out)  # evolve follows the same folder instead of starting over in the root
        self.assertFalse((self.ws / "state.json").exists())
        self.assertIn("report-total", out)

    def test_merge_skips_rows_already_in_the_tasks_file(self):
        self.add_feedback(1)
        (row,) = pending(self.suite)
        with (self.suite / "tasks.jsonl").open("a", encoding="utf-8") as f:  # merged before a crash
            f.write(json.dumps({**row, "split": "train"}) + "\n")
        (self.suite / MERGED).write_text(json.dumps(row) + "\n", encoding="utf-8")  # logged before the crash too
        self.assertEqual(merge_pending(self.suite, self.suite / "tasks.jsonl"), 0)
        self.assertEqual(len(load_suite(self.suite).tasks), 9)
        self.assertEqual(len(read_text(self.suite / MERGED).splitlines()), 1)
        self.assertEqual(pending(self.suite), [])

    def test_nothing_to_do_still_exports_and_takes_over_manual_exports(self):
        self.assertEqual(self.improve()[0], 0)
        exported = self.dest / "report-total" / "SKILL.md"
        code, out = run_cli("export", self.ws, "--dest", self.dest, "--force")
        self.assertEqual(code, 0, out)
        self.assertIn(EXPORT_MARK, read_text(exported))
        self.assertNotIn(FEEDBACK_HEADING, read_text(exported))
        code, out = self.improve()  # nothing to do, but the manual export gets the feedback footer back
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do", out)
        self.assertIn(FEEDBACK_HEADING, read_text(exported))
        exported.unlink()
        self.assertEqual(self.improve()[0], 0)
        self.assertIn(FEEDBACK_HEADING, read_text(exported))

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
