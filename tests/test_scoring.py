import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from wikiskill.config import Suite, Task
from wikiskill.rollout import RolloutResult
from wikiskill.scoring import (HumanReviewPending, Scorer, extract_answer, parse_number, parse_verdict,
                               read_review_rows, read_review_sheet, write_review_sheet)


def make_suite(root: Path, scorer: dict, human: bool = False) -> Suite:
    return Suite("s", root, "desc", "{skill_section}", scorer, 1.0, human, root / "t.jsonl", root / "files", ())


def result(pred: str, ok: bool = True, workdir: str = "w") -> RolloutResult:
    return RolloutResult("t", f"reasoning <answer>{pred}</answer>", pred, ok, "" if ok else "boom", workdir, ())


class FakeJudge:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, prompt, system=None):
        self.prompts.append(prompt)
        return self.reply


class AnswerParsingTest(unittest.TestCase):
    def test_extract_answer_takes_last_tag(self):
        self.assertEqual(extract_answer("<answer>1</answer> then <ANSWER> 2 </ANSWER>"), "2")
        self.assertEqual(extract_answer("plain text"), "plain text")
        self.assertEqual(extract_answer(""), "")

    def test_parse_number(self):
        self.assertEqual(parse_number("合計は１，２３４円"), 1234.0)  # full-width digits and comma
        self.assertEqual(parse_number("−5.5%"), -5.5)
        self.assertIsNone(parse_number("なし"))
        self.assertEqual(parse_number(".5"), 0.5)
        self.assertEqual(parse_number("-.25"), -0.25)


class AutoScorerTest(unittest.TestCase):
    def scorer(self, scorer, root=Path(".")):
        return Scorer(make_suite(root, scorer))

    def test_simple_scorers(self):
        task = Task("t", "train", "q", answer=["東京", "Tokyo"])
        self.assertEqual(self.scorer({"type": "exact"}).auto(task, result(" ＴＯＫＹＯ "))[0], 1.0)
        self.assertEqual(self.scorer({"type": "exact"}).auto(task, result("Osaka"))[0], 0.0)
        self.assertEqual(self.scorer({"type": "contains"}).auto(task, result("答えは東京です"))[0], 1.0)
        regex_task = Task("t", "train", "q", answer=r"^\d{4}-\d{2}$")
        self.assertEqual(self.scorer({"type": "regex"}).auto(regex_task, result("2026-10"))[0], 1.0)
        num_task = Task("t", "train", "q", answer="1234")
        self.assertEqual(self.scorer({"type": "number"}).auto(num_task, result("1,234.01"))[0], 0.0)
        self.assertEqual(self.scorer({"type": "number", "abs_tol": 0.05}).auto(num_task, result("1,234.01"))[0], 1.0)

    def test_failed_run_scores_zero(self):
        task = Task("t", "train", "q", answer="x")
        score, note = self.scorer({"type": "exact"}).auto(task, result("x", ok=False))
        self.assertEqual(score, 0.0)
        self.assertIn("boom", note)

    def test_script_scorer_gets_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "check.py").write_text(
                "from pathlib import Path\n"
                "def score(task, prediction, workdir):\n"
                "    ok = (Path(workdir) / 'out.txt').read_text(encoding='utf-8').strip() == task['answer']\n"
                "    return (1.0 if ok else 0.0), 'checked out.txt'\n", encoding="utf-8")
            (root / "w").mkdir()
            (root / "w" / "out.txt").write_text("42", encoding="utf-8")
            scorer = self.scorer({"type": "script", "path": "check.py"}, root)
            self.assertEqual(scorer.auto(Task("t", "train", "q", answer="42"), result("", workdir=str(root / "w"))),
                             (1.0, "checked out.txt"))

    def test_script_errors_score_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "check.py").write_text(
                "def score(task, prediction, workdir):\n    return open(workdir + '/missing.txt').read()\n",
                encoding="utf-8")
            score, note = self.scorer({"type": "script", "path": "check.py"}, root).auto(
                Task("t", "train", "q"), result("", workdir=tmp))
            self.assertEqual(score, 0.0)
            self.assertIn("FileNotFoundError", note)

    def test_llm_judge_nan_is_zero(self):
        scorer = Scorer(make_suite(Path("."), {"type": "llm_judge"}), FakeJudge('{"score": NaN}'))
        self.assertEqual(scorer.auto(Task("t", "train", "q"), result("x"))[0], 0.0)

    def test_llm_judge(self):
        task = Task("t", "train", "訳して", answer="apple", rubric="一語で")
        judge = FakeJudge('```json\n{"score": 1.7, "reason": "fine"}\n```')
        scorer = Scorer(make_suite(Path("."), {"type": "llm_judge", "rubric": "用語集: 見積書=quotation"}), judge)
        self.assertEqual(scorer.auto(task, result("Apple")), (1.0, "fine"))  # clamped to [0, 1]
        self.assertIn("一語で", judge.prompts[0])  # per-task rubric ...
        self.assertIn("見積書=quotation", judge.prompts[0])  # ... is added to the shared one
        bad = Scorer(make_suite(Path("."), {"type": "llm_judge"}), FakeJudge("no json"))
        self.assertEqual(bad.auto(task, result("Apple"))[0], 0.0)


class HumanReviewTest(unittest.TestCase):
    def test_verdict_words(self):
        for word in ("OK", "ok", "○", "合格", "1"):
            self.assertEqual(parse_verdict(word), 1.0)
        for word in ("NG", "要修正", "×", "0"):
            self.assertEqual(parse_verdict(word), 0.0)
        self.assertEqual(parse_verdict("0.5"), 0.5)
        self.assertIsNone(parse_verdict("  "))
        for bad in ("maybe", "2"):
            with self.assertRaises(ValueError):
                parse_verdict(bad)

    def test_pause_fill_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = make_suite(root, {"type": "exact"}, human=True)
            tasks = [Task("a", "val", "質問A", answer="1"), Task("b", "val", "質問B")]
            results = {"a": replace(result("1"), task_id="a"), "b": replace(result("x"), task_id="b")}
            sheet = root / "reviews" / "val.csv"
            scorer = Scorer(suite)
            with self.assertRaises(HumanReviewPending):
                scorer.score_all(tasks, results, sheet)
            self.assertTrue(sheet.read_bytes().startswith(b"\xef\xbb\xbf"))  # BOM for Excel
            rows = sheet.read_text(encoding="utf-8-sig").splitlines()
            self.assertTrue(rows[1].startswith("id:a,"))  # prefix stops Excel converting ids
            self.assertIn("質問A", rows[1])
            self.assertIn(",1,", rows[1])  # auto_score hint for a task with an answer
            row_a, row_b = read_review_rows(sheet)
            write_review_sheet(sheet, [dict(row_a, verdict="OK")])  # reviewer deleted row b by mistake
            with self.assertRaises(HumanReviewPending) as pending:
                scorer.score_all(tasks, results, sheet)
            self.assertEqual(pending.exception.missing, ["b"])
            self.assertIn("id:b", sheet.read_text(encoding="utf-8-sig"))  # deleted row is re-added
            write_review_sheet(sheet, [dict(row_a, verdict="OK"),
                                       dict(row_b, verdict="要修正", comment="単位が\n抜けている")])
            outcomes = scorer.score_all(tasks, results, sheet)
            self.assertTrue(outcomes["a"].passed)
            self.assertFalse(outcomes["b"].passed)
            self.assertIn("単位が\n抜けている", outcomes["b"].note)  # multi-line cell kept

    def test_verdict_for_an_old_answer_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorer = Scorer(make_suite(root, {"type": "exact"}, human=True))
            tasks, sheet = [Task("a", "val", "q")], root / "r.csv"
            with self.assertRaises(HumanReviewPending):
                scorer.score_all(tasks, {"a": replace(result("old"), task_id="a")}, sheet)
            write_review_sheet(sheet, [dict(read_review_rows(sheet)[0], verdict="OK")])
            self.assertTrue(scorer.score_all(tasks, {"a": replace(result("old"), task_id="a")}, sheet)["a"].passed)
            with self.assertRaises(HumanReviewPending):  # a new answer needs a new verdict
                scorer.score_all(tasks, {"a": replace(result("new"), task_id="a")}, sheet)

    def test_formula_like_cells_are_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "r.csv"
            write_review_sheet(sheet, [{"task_id": "id:a", "verdict": "OK", "agent_answer": "=HYPERLINK(1)",
                                        "comment": "-16000", "prompt": "'=already quoted"}])
            self.assertIn("'=HYPERLINK", sheet.read_text(encoding="utf-8-sig"))
            row = read_review_rows(sheet)[0]
            self.assertEqual((row["agent_answer"], row["comment"], row["prompt"]),
                             ("=HYPERLINK(1)", "-16000", "'=already quoted"))  # exact round trip

    def test_reads_excel_shift_jis_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "r.csv"
            sheet.write_bytes("task_id,verdict,comment\nt1,要修正,桁が違う\n".encode("cp932"))
            self.assertEqual(read_review_sheet(sheet), {"t1": (0.0, "reviewer: 桁が違う")})


if __name__ == "__main__":
    unittest.main()
