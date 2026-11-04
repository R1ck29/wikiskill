"""Learning from real usage: `feedback` (pending queue) and `improve` (rounds in one workspace)."""
import contextlib
import json
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeBackend, TempDirTest
from tests.test_cli import run_cli
from tests.test_evolve import write_suite
from wikiskill import cli
from wikiskill.backends import Budget, BudgetExceeded
from wikiskill.config import ConfigError, load_suite
from wikiskill.evolve import has_work, is_done
from wikiskill.improve import (
    AUTO,
    BACKGROUND_ERR,
    FEEDBACK,
    MERGED,
    automatic_options,
    background_log,
    load_settings,
    merge_pending,
    merge_ready,
    pending,
    record_feedback,
    start_improve,
    trim_log,
)
from wikiskill.skills import EXPORT_MARK
from wikiskill.util import read_text, try_lock, unlock
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
        self.started = mock.Mock()  # stands in for the background run, which tests start by hand
        for target, value in (("wikiskill.cli.make_backend", lambda *a, **k: FakeBackend()),
                              ("wikiskill.cli.start_improve", self.started)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def improve(self, *argv):
        return run_cli("improve", self.suite, "--workspace", self.ws, "--export", self.dest, "--workers", 1, *argv)

    def add_feedback(self, n, output="TOTAL"):
        outs = []
        for i in range(n):
            code, out = run_cli("feedback", self.suite, "--verdict", "good", "--prompt", f"real {i} {output}",
                                "--output", output)
            self.assertEqual(code, 0, out)
            outs.append(out)
        return outs

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
        self.assertIn("started: `improve` runs in the background", out)  # the third row starts improve itself
        self.started.assert_called_once()
        code, out = self.improve()  # what the background run does
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

    def test_the_automatic_start_repeats_the_saved_improve(self):
        self.add_feedback(2)  # before the first improve nothing knows how to run it: the caller is told to
        self.assertIn("ready: run `improve`", self.add_feedback(1)[-1])
        self.started.assert_not_called()
        self.assertEqual(self.improve("--model", "m1", "--min-new", 2)[0], 0)
        settings = load_settings(self.suite)
        self.assertEqual((settings["workspace"], settings["min_new"]), (str(self.ws.resolve()), 2))
        self.assertIn(str(self.dest.resolve()), settings["args"])
        self.assertEqual(self.add_feedback(1)[-1].count("started"), 0)  # 1 of --min-new 2
        out = self.add_feedback(1)[-1]
        self.assertIn(f"log: {self.ws.resolve() / 'background.log'}", out)
        suite, saved = self.started.call_args[0]
        self.assertEqual((suite.root, saved), (self.suite.resolve(), settings))
        options = automatic_options(saved)
        self.assertEqual((options["--model"], options["--daily-budget"]), ("claude-sonnet-5-5", "10.0"))
        code, out = run_cli("improve", self.suite, "--workspace", saved["workspace"], "--min-new", 2,
                            *[item for pair in options.items() for item in pair], "--wait", "--automatic")
        self.assertEqual(code, 0, out)  # (what start_improve runs)
        self.assertIn("merged feedback: 2", out)
        self.assertIn("exported report-total", out)
        self.assertEqual(load_settings(self.suite), settings)  # the person's options, not Sonnet and $10
        (self.suite / AUTO).unlink()  # turned off while that run waited or ran
        self.assertEqual(run_cli("improve", self.suite, "--workspace", saved["workspace"], "--min-new", 2,
                                 *[item for pair in options.items() for item in pair], "--wait", "--automatic")[0], 0)
        self.assertFalse((self.suite / AUTO).exists())  # it stays off
        self.assertEqual(self.improve("--model", "m2")[0], 0)  # a manual improve saves its own
        self.assertIn("m2", load_settings(self.suite)["args"])

    def test_automatic_runs_use_sonnet_on_claude_and_keep_a_saved_budget(self):
        options = automatic_options({"args": ["--backend", "codex", "--model", "gpt-x", "--daily-budget", "2.5"]})
        self.assertEqual(options, {"--backend": "codex", "--model": "gpt-x", "--daily-budget": "2.5"})
        self.assertEqual(automatic_options({"args": []}), {"--model": "claude-sonnet-5-5", "--daily-budget": "10.0"})
        for bad in ("-1", "0", "nan", "inf", "abc"):  # a hand-edited improve.json
            self.assertEqual(automatic_options({"args": ["--daily-budget", bad]})["--daily-budget"], "10.0")

    def test_a_spent_budget_pauses_improve_and_feedback(self):
        self.assertEqual(self.improve()[0], 0)
        Budget(Workspace(self.ws).spend_path, 1.0).add(1.5)
        self.add_feedback(2)
        self.assertIn("started", self.add_feedback(1)[-1])  # $1.50 of the automatic $10 is spent
        self.started.assert_called_once()
        Budget(Workspace(self.ws).spend_path, 1.0).add(9.0)
        out = self.add_feedback(1)[-1]
        self.assertIn("paused: today's budget of $10.00 is spent ($10.50)", out)
        self.started.assert_called_once()  # not started again
        code, out = self.improve("--daily-budget", 10)
        self.assertEqual(code, 0, out)
        self.assertIn("paused:", out)
        self.assertEqual(len(pending(self.suite)), 4)  # nothing merged or run
        self.assertIn("--daily-budget", load_settings(self.suite)["args"])  # a pause is no failure
        code, out = self.improve("--daily-budget", 20)
        self.assertEqual(code, 0, out)
        self.assertIn("merged feedback: 4", out)

    def test_a_round_paused_after_the_merge_resumes_with_the_next_single_row(self):
        self.assertEqual(self.improve()[0], 0)
        self.add_feedback(3)
        with mock.patch("wikiskill.cli._evolve_round", side_effect=BudgetExceeded("today's budget is spent")):
            self.assertIn("paused:", self.improve("--daily-budget", 10)[1])
        self.assertEqual(pending(self.suite), [])  # the rows are merged: the queue alone would wait for 3 more
        self.started.reset_mock()
        self.assertIn("started", self.add_feedback(1)[-1])  # the unfinished round starts again at once
        self.started.assert_called_once()
        self.assertIn("merged feedback: 0", self.improve()[1])  # and finishes
        self.started.reset_mock()
        self.add_feedback(1)
        self.started.assert_not_called()  # a finished round waits for --min-new rows again
        Workspace(self.ws).state_path.write_text("{broken", encoding="utf-8")
        self.assertIn("recorded", self.add_feedback(1)[-1])  # a damaged state never blocks recording

    def test_a_budget_spent_mid_round_keeps_the_progress_and_exports(self):
        real_round = cli._evolve_round

        def round_until_spent(args, ws, merged, budget):
            real_round(args, ws, merged, budget)
            raise BudgetExceeded("today's budget of $1.00 is spent ($1.20)")

        with mock.patch("wikiskill.cli._evolve_round", round_until_spent):
            code, out = self.improve("--daily-budget", 1)
        self.assertEqual(code, 0, out)
        self.assertIn("paused: today's budget of $1.00 is spent", out)
        self.assertIn("exported report-total", out)
        self.assertIsNotNone(self.state())

    def test_a_failed_improve_is_reported_by_the_next_feedback(self):
        self.assertEqual(self.improve()[0], 0)
        self.add_feedback(3)
        with mock.patch("wikiskill.cli._evolve_round", side_effect=RuntimeError("claude exited 1: quota")):
            code, out = self.improve()
        self.assertEqual(code, 1, out)
        error = load_settings(self.suite)["last_error"]
        self.assertEqual(error["message"], "claude exited 1: quota")
        out = self.add_feedback(3)[-1]
        self.assertIn(f"warning: the last improve failed at {error['time']}: ", out)
        self.assertIn("started", out)  # the cause may be gone: the next run is still started
        self.assertEqual(self.improve()[0], 0)
        self.assertNotIn("last_error", load_settings(self.suite))  # a successful run clears it
        self.assertNotIn("warning", self.add_feedback(1)[-1])

    def test_a_pause_keeps_the_last_failure_until_a_run_gets_through(self):
        self.assertEqual(self.improve()[0], 0)
        self.add_feedback(3)
        with mock.patch("wikiskill.cli._evolve_round", side_effect=RuntimeError("quota")):
            self.assertEqual(self.improve()[0], 1)
        with mock.patch("wikiskill.cli._evolve_round", side_effect=BudgetExceeded("today's budget is spent")):
            self.assertIn("paused:", self.improve()[1])
        self.assertEqual(load_settings(self.suite)["last_error"]["message"], "quota")
        self.assertIn("warning: the last improve failed", self.add_feedback(1)[-1])
        self.assertEqual(self.improve()[0], 0)
        self.assertNotIn("last_error", load_settings(self.suite))

    def test_a_background_start_that_died_early_is_reported(self):
        self.assertEqual(self.improve()[0], 0)
        err = self.ws / BACKGROUND_ERR
        err.write_text("Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n",
                       encoding="utf-8")
        out = self.add_feedback(1)[-1]
        self.assertIn("warning: the last background start wrote errors before improve opened its log: "
                      "ModuleNotFoundError: No module named 'x'", out)
        err.write_text("", encoding="utf-8")  # what the next start leaves when it gets going
        self.assertNotIn("warning", self.add_feedback(1)[-1])

    def test_a_failure_is_recorded_for_a_suite_json_path_and_never_hides_the_error(self):
        self.assertEqual(self.improve()[0], 0)
        self.add_feedback(3)
        with mock.patch("wikiskill.cli._evolve_round", side_effect=ConfigError("bad rubric")):
            code, _ = run_cli("improve", self.suite / "suite.json", "--workspace", self.ws, "--workers", 1)
        self.assertEqual(code, 2)
        self.assertEqual(load_settings(self.suite)["last_error"]["message"], "bad rubric")
        with mock.patch("wikiskill.cli._evolve_round", side_effect=ConfigError("bad rubric")), \
                mock.patch("wikiskill.improve.write_json", side_effect=OSError("disk full")):
            code, out = self.improve()
        self.assertEqual(code, 2)  # the original error and its exit code, not the failed note
        self.assertIn("error: bad rubric", out)

    def test_human_review_turns_the_automatic_start_off(self):
        self.assertEqual(self.improve()[0], 0)
        self.assertTrue((self.suite / AUTO).is_file())
        cfg = json.loads(read_text(self.suite / "suite.json"))
        (self.suite / "suite.json").write_text(json.dumps({**cfg, "human_review": True}), encoding="utf-8")
        self.assertEqual(self.improve()[0], 0)  # nothing to do, so nobody is asked to score yet
        self.assertFalse((self.suite / AUTO).exists())
        self.assertIn("ready: run `improve`", self.add_feedback(3)[-1])
        self.started.assert_not_called()

    def test_feedback_recorded_during_a_round_starts_the_next_one(self):
        real_round = cli._evolve_round

        def round_with_feedback(args, ws, merged, budget):
            if not pending(self.suite) and self.state() is None:  # while the first round runs
                self.add_feedback(3)
            real_round(args, ws, merged, budget)

        with mock.patch("wikiskill.cli._evolve_round", round_with_feedback):
            code, out = self.improve()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("merged feedback:"), 2)
        self.assertIn("merged feedback: 3", out)
        self.assertEqual(pending(self.suite), [])
        self.assertEqual(self.state()["history"][-1]["outcome"], "new_tasks")

    def test_a_queued_run_steps_aside_for_the_one_already_waiting(self):
        Workspace(self.ws).init()
        running, waiting = try_lock(self.ws / "evolve.lock"), try_lock(self.ws / "improve.wait")
        try:
            code, out = self.improve("--wait")
            self.assertEqual(code, 0, out)
            self.assertIn("already waits", out)
            self.assertIsNone(self.state())
            code, out = self.improve()
            self.assertEqual(code, 1, out)  # without --wait a running workspace is an error
            self.assertIn("another evolve or improve is running", out)
        finally:
            unlock(running)
            unlock(waiting)

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

    def test_feedback_sees_spend_and_progress_of_a_legacy_generation(self):
        self.assertEqual(self.improve()[0], 0)  # settings keep the path the user passed
        gen = self.ws / "gen-002"  # 0.2.x layout: the run lives here, not in the root
        gen.mkdir()
        state = json.loads(read_text(self.ws / "state.json"))
        (self.ws / "state.json").unlink()
        state.update(iteration=1, round_start=1, r_best=0.25, stage="rollout")
        Workspace(gen).save_state(state)
        Budget(Workspace(gen).spend_path, 10).add(10)  # the automatic run's $10, recorded where improve writes it
        self.started.reset_mock()
        out = self.add_feedback(1)[-1]  # one row: only an unfinished round would look at the budget
        self.assertIn("paused: today's budget of $10.00 is spent ($10.00)", out)
        self.started.assert_not_called()
        self.assertEqual(background_log(load_settings(self.suite)), self.ws.resolve() / "background.log")

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

    def test_feedback_recorded_while_merging_is_never_lost(self):
        suite = load_suite(self.suite, require_answers=False)
        writers = [threading.Thread(target=record_feedback, args=(suite, f"p{i}", "good", "TOTAL"))
                   for i in range(12)]
        merged = 0
        for w in writers:
            w.start()
        while any(w.is_alive() for w in writers):
            merged += merge_pending(self.suite, self.suite / "tasks.jsonl")
        for w in writers:
            w.join()
        merged += merge_pending(self.suite, self.suite / "tasks.jsonl")
        self.assertEqual(merged, 12)
        self.assertEqual(len(load_suite(self.suite).tasks), 8 + 12)
        self.assertEqual(len(read_text(self.suite / MERGED).splitlines()), 12)

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


class BackgroundStartTest(TempDirTest):
    def test_the_detached_run_finds_the_package_and_logs_its_output(self):
        suite = write_suite(self.root / "suite")
        settings = {"workspace": str(self.root / "ws"), "min_new": 3,
                    "args": ["--backend-config", str(self.root / "missing.json"), "--workers", "1"]}
        process = start_improve(load_suite(suite), settings)  # fails at once, before any model call
        self.assertEqual(process.wait(timeout=120), 2)
        log = read_text(background_log(settings))
        self.assertIn("started by feedback", log)
        self.assertRegex(log, r"error: .*missing\.json")  # printed by the improve command itself
        self.assertEqual(read_text(self.root / "ws" / BACKGROUND_ERR), "")  # it reached its own log
        self.assertIsNone(load_settings(suite))  # a failed run is never repeated automatically

    def test_output_from_before_the_improve_command_lands_in_the_error_file(self):
        suite = write_suite(self.root / "suite")
        settings = {"workspace": str(self.root / "ws"), "min_new": 3,
                    "args": ["--backend-config", str(self.root / "missing.json")]}
        with mock.patch.dict("os.environ", {"PYTHONWARNINGS": "bogus"}):  # Python itself complains at startup
            process = start_improve(load_suite(suite), settings)
        self.assertEqual(process.wait(timeout=120), 2)
        self.assertIn("Invalid -W option ignored", read_text(self.root / "ws" / BACKGROUND_ERR))
        self.assertRegex(read_text(background_log(settings)), r"error: .*missing\.json")

    def test_a_usage_error_of_the_detached_run_reaches_its_log(self):
        log = self.root / "background.log"
        result = subprocess.run([sys.executable, "-m", "wikiskill", "improve", str(self.root), "--workspace",
                                 str(self.root / "ws"), f"--log={log}", "--no-such-option"],
                                cwd=str(Path(cli.__file__).resolve().parent.parent), capture_output=True, text=True,
                                check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertIn("unrecognized arguments: --no-such-option", read_text(log))

    def test_a_long_log_keeps_only_its_last_whole_lines(self):
        log = self.root / "background.log"
        log.write_bytes(b"".join(b"line %04d\n" % i for i in range(1000)))  # 10 bytes a line
        trim_log(log, limit=20000)
        self.assertEqual(log.stat().st_size, 10000)
        last_lines = b"".join(b"line %04d\n" % i for i in range(991, 1000))
        trim_log(log, limit=5000, keep=95)
        self.assertEqual(log.read_bytes(), last_lines)
        log.write_bytes(b"x" * 6000 + b"\n" + last_lines)
        trim_log(log, limit=5000, keep=90)  # the cut is already at a line start: no line is lost
        self.assertEqual(log.read_bytes(), last_lines)
        log.write_bytes(b"x" * 6000)
        trim_log(log, limit=5000, keep=90)  # no whole line in the tail
        self.assertEqual(log.read_bytes(), b"")
        trim_log(self.root / "missing.log")  # nothing to do


if __name__ == "__main__":
    unittest.main()
