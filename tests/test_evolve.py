import json
import tempfile
import unittest
from pathlib import Path

from tests.fakes import FakeBackend
from wikiskill.config import load_suite
from wikiskill.evaluate import evaluate, paired_bootstrap
from wikiskill.evolve import EvolveOptions, Evolution
from wikiskill.review import apply_sheet, collect
from wikiskill.scoring import HumanReviewPending, read_review_rows, write_review_sheet
from wikiskill.workspace import Workspace


def write_suite(root: Path, human_review: bool = False, with_answers: bool = True) -> Path:
    tasks = [{"id": f"t{i}", "split": "train", "prompt": f"Sales question {i}"} for i in range(1, 5)]
    tasks += [{"id": f"v{i}", "split": "val", "prompt": f"Val question {i}"} for i in range(1, 3)]
    tasks += [{"id": f"x{i}", "split": "test", "prompt": f"Test question {i}"} for i in range(1, 3)]
    for t in tasks:
        if with_answers:
            t["answer"] = "TOTAL"
    root.mkdir(parents=True, exist_ok=True)
    (root / "tasks.jsonl").write_text("\n".join(json.dumps(t) for t in tasks) + "\n", encoding="utf-8")
    (root / "suite.json").write_text(json.dumps({
        "name": "sales", "task_description": "sales questions", "tasks": "tasks.jsonl",
        "system_prompt": "Answer.\n{skill_section}", "scorer": {"type": "exact"}, "human_review": human_review,
    }), encoding="utf-8")
    return root


class EvolveLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.opts = EvolveOptions(iterations=4, workers=2)

    def test_reject_then_accept_then_early_stop(self):
        suite = load_suite(write_suite(self.root / "suite"))
        ws = Workspace(self.root / "ws")
        state = Evolution(suite, FakeBackend(), ws, self.opts).run()
        self.assertEqual(state["baseline"], 0.0)
        self.assertEqual([h["outcome"] for h in state["history"]], ["rejected", "accepted"])
        self.assertEqual(state["r_best"], 1.0)
        self.assertEqual(state["iteration"], 3)  # stopped early: R_best = 1.0
        self.assertEqual(sorted(p.name for p in ws.skills.iterdir()), ["report-total"])  # rejected skill rolled back
        impact = (ws.wiki / "skill-impact.md").read_text(encoding="utf-8")
        self.assertIn("## Iteration 1 - REJECTED", impact)
        self.assertIn("+name: be-careful", impact)  # full diff of the rejected proposal is kept
        self.assertIn("## Iteration 2 - ACCEPTED", impact)
        self.assertIn("Patterns: partial-answers.md", impact)  # PURPOSE.md of the proposal is kept
        self.assertTrue((ws.wiki / "patterns" / "partial-answers.md").is_file())  # wiki kept after rejection
        logs = (ws.wiki / "logs.md").read_text(encoding="utf-8")
        self.assertEqual(logs.count("## Iteration "), 2)
        self.assertTrue((ws.raw_dir(1) / "traces" / "t1.md").is_file())
        self.assertIn("Outcome: FAIL", (ws.raw_dir(1) / "traces" / "t1.md").read_text(encoding="utf-8"))

    def test_resume_after_crash_does_not_repeat_work(self):
        suite = load_suite(write_suite(self.root / "suite"))
        ws = Workspace(self.root / "ws")
        backend = FakeBackend(crash_proposer_once=True)
        with self.assertRaises(RuntimeError):
            Evolution(suite, backend, ws, self.opts).run()
        self.assertEqual(ws.load_state()["stage"], "propose")
        calls_before = backend.agent_calls
        state = Evolution(suite, backend, ws, self.opts).run()
        self.assertEqual([h["outcome"] for h in state["history"]], ["rejected", "accepted"])
        logs = (ws.wiki / "logs.md").read_text(encoding="utf-8")
        self.assertEqual(logs.count("## Iteration 1\n"), 1)  # maintainer not re-run on resume
        # resume re-used iteration 1 train rollouts: only val(2) + iter 2 train(4) + val(2) ran again
        self.assertEqual(backend.agent_calls - calls_before, 8)

    def test_human_review_pauses_and_can_be_switched_off(self):
        suite_dir = write_suite(self.root / "suite", human_review=True, with_answers=False)
        ws = Workspace(self.root / "ws")
        with self.assertRaises(HumanReviewPending) as pending:
            Evolution(load_suite(suite_dir), FakeBackend(), ws, self.opts).run()
        sheet = pending.exception.sheet
        self.assertEqual(sheet.name, "baseline_val.csv")
        rows = read_review_rows(sheet)
        for row in rows:
            row["verdict"] = "NG"
        write_review_sheet(sheet, rows)
        with self.assertRaises(HumanReviewPending) as pending:
            Evolution(load_suite(suite_dir), FakeBackend(), ws, self.opts).run()
        self.assertEqual(pending.exception.sheet.name, "iter_001_train.csv")
        # switch human review off: add reference answers and continue automatically
        write_suite(self.root / "suite", human_review=False, with_answers=True)
        state = Evolution(load_suite(suite_dir), FakeBackend(), ws, self.opts).run()
        self.assertEqual(state["r_best"], 1.0)

    def test_evaluate_compare_and_hybrid_review(self):
        suite_dir = write_suite(self.root / "suite", with_answers=False)
        suite = load_suite(suite_dir, require_answers=False)
        sheet = collect(suite, FakeBackend(), ["train", "val", "test"], self.root / "review", workers=2)
        rows = read_review_rows(sheet)
        for row in rows:
            row["verdict"], row["corrected_answer"] = "NG", "TOTAL"
        rows[0]["rubric_note"] = "合計行を答える"
        write_review_sheet_hybrid(sheet, rows)
        counts = apply_sheet(suite, sheet)
        self.assertEqual(counts["answer_from_correction"], 8)
        self.assertTrue((suite_dir / "tasks.jsonl.bak").is_file())
        suite = load_suite(suite_dir)  # answers now present -> automatic scoring works
        self.assertIn("合計行", suite.tasks[0].rubric)
        ws = Workspace(self.root / "ws")
        Evolution(suite, FakeBackend(), ws, self.opts).run()
        base = evaluate(suite, FakeBackend(), None, "test", self.root / "eval_base", 2)
        tuned = evaluate(suite, FakeBackend(), ws.skills, "test", self.root / "eval_skills", 2)
        self.assertEqual((base["mean_score"], tuned["mean_score"]), (0.0, 1.0))
        result = paired_bootstrap(base["scores"], tuned["scores"])
        self.assertEqual(result["difference_b_minus_a"], 1.0)
        self.assertEqual(result["p_value"], 0.0)

    def test_evaluate_reruns_when_skills_change(self):
        suite = load_suite(write_suite(self.root / "suite"))
        ws = Workspace(self.root / "ws")
        Evolution(suite, FakeBackend(), ws, self.opts).run()
        out = self.root / "eval"
        self.assertEqual(evaluate(suite, FakeBackend(), None, "test", out, 1)["mean_score"], 0.0)
        self.assertEqual(evaluate(suite, FakeBackend(), ws.skills, "test", out, 1)["mean_score"], 1.0)
        backend = FakeBackend()
        evaluate(suite, backend, ws.skills, "test", out, 1)
        self.assertEqual(backend.agent_calls, 0)  # identical prompt and skills: cache reused

    def test_changed_task_ids_are_refused(self):
        suite_dir = write_suite(self.root / "suite")
        ws = Workspace(self.root / "ws")
        Evolution(load_suite(suite_dir), FakeBackend(), ws, EvolveOptions(iterations=1, workers=1)).run()
        with open(suite_dir / "tasks.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "t9", "split": "train", "prompt": "new", "answer": "TOTAL"}) + "\n")
        with self.assertRaises(ValueError):
            Evolution(load_suite(suite_dir), FakeBackend(), ws, self.opts).run()

    def test_interrupted_wiki_backup_is_not_restored(self):
        from wikiskill.evolve import _copy_atomic
        src, dst = self.root / "wiki", self.root / "backup"
        (src / "patterns").mkdir(parents=True)
        (src / "index.md").write_text("index", encoding="utf-8")
        partial = self.root / "backup.partial"
        partial.mkdir()  # left over from a copy that was interrupted
        _copy_atomic(src, dst)
        self.assertEqual((dst / "index.md").read_text(encoding="utf-8"), "index")
        self.assertFalse(partial.exists())

    def test_review_apply_twice_keeps_original_backup(self):
        suite_dir = write_suite(self.root / "suite", with_answers=False)
        suite = load_suite(suite_dir, require_answers=False)
        original = (suite_dir / "tasks.jsonl").read_text(encoding="utf-8")
        sheet = collect(suite, FakeBackend(), ["train"], self.root / "review", workers=1)
        rows = read_review_rows(sheet)
        rows[0].update(verdict="OK", rubric_note="合計を答える")
        rows[1].update(verdict="OK", agent_answer="(failed) timeout")
        write_review_sheet_hybrid(sheet, rows)
        first = apply_sheet(suite, sheet)
        second = apply_sheet(suite, sheet)
        self.assertEqual((first["answer_from_ok"], first["rubric_added"]), (1, 1))
        self.assertEqual(second["rubric_added"], 0)
        self.assertEqual((suite_dir / "tasks.jsonl.bak").read_text(encoding="utf-8"), original)
        task = load_suite(suite_dir, require_answers=False).tasks
        self.assertEqual(task[0].rubric, "合計を答える")
        self.assertIsNone(task[1].answer)  # a failed run's text is never taken as the answer

    def test_needs_train_and_val(self):
        suite_dir = write_suite(self.root / "suite")
        lines = [l for l in (suite_dir / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if '"val"' not in l]
        (suite_dir / "tasks.jsonl").write_text("\n".join(lines), encoding="utf-8")
        with self.assertRaises(ValueError):
            Evolution(load_suite(suite_dir), FakeBackend(), Workspace(self.root / "ws"), self.opts)


def write_review_sheet_hybrid(path, rows):
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
