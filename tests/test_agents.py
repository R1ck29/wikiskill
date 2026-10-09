import json
import tempfile
import unittest
from pathlib import Path

from tests.fakes import GOOD_SKILL
from wikiskill.config import Suite, Task
from wikiskill.maintainer import apply_output, build_prompt, run_maintainer, sample_traces
from wikiskill.proposer import ReadSandbox, outcome_summary, run_proposer
from wikiskill.scoring import Outcome
from wikiskill.workspace import Workspace


def outcomes(n_fail, n_pass):
    result = {f"f{i}": Outcome(f"f{i}", 0.0, False, "x") for i in range(n_fail)}
    result.update({f"p{i}": Outcome(f"p{i}", 1.0, True, "y") for i in range(n_pass)})
    return result


class Replies:
    """complete() returns the queued replies in order."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, system=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class MaintainerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name))
        self.ws.init()

    def test_sampling_is_stratified_and_capped(self):
        picked = sample_traces(outcomes(9, 7), seed=1)
        self.assertEqual(sum(t.startswith("f") for t in picked), 5)
        self.assertEqual(sum(t.startswith("p") for t in picked), 3)
        self.assertEqual(sample_traces(outcomes(9, 7), seed=1), picked)  # deterministic
        self.assertEqual(len(sample_traces(outcomes(2, 0), seed=1)), 2)

    def test_prompt_caps_each_trace(self):
        prompt = build_prompt(self.ws, 1, {"t1": "A" * 40000}, [("s", "SKILL")])
        self.assertLess(prompt.count("A"), 15000)
        self.assertIn("characters truncated", prompt)
        self.assertIn("### wiki/index.md", prompt)
        self.assertIn("skills/s/SKILL.md", prompt)

    def test_apply_output_skips_bad_entries(self):
        (self.ws.wiki / "patterns" / "old.md").write_text("Evidence: iter 0\n", encoding="utf-8")
        warnings = apply_output(self.ws, 2, {
            "create_patterns": [{"name": "new-one.md", "content": "# New"},
                                {"name": "../escape.md", "content": "x"},
                                {"name": "old.md", "content": "dup"},
                                {"name": "number.md", "content": 5}],
            "update_patterns": [{"name": "old.md", "edits": [{"op": "append", "content": "Evidence: iter 2"}]},
                                {"name": "old.md", "edits": [{"op": "replace", "target": "missing", "content": "y"}]},
                                {"name": "ghost.md", "edits": [{"op": "append", "content": "z"}]}],
            "update_index": "# Wiki Index\n- [new-one](wiki/patterns/new-one.md): x\n",
            "append_log": "Iteration two findings.",
        })
        self.assertEqual(len(warnings), 5)
        self.assertFalse((self.ws.root / "escape.md").exists())
        old = (self.ws.wiki / "patterns" / "old.md").read_text(encoding="utf-8")
        self.assertIn("Evidence: iter 2", old)
        self.assertIn("dup", old)  # duplicate create was appended, not overwritten
        self.assertIn("Evidence: iter 0", old)
        logs = (self.ws.wiki / "logs.md").read_text(encoding="utf-8")
        self.assertIn("## Iteration 2", logs)
        self.assertIn("(harness)", logs)

    def test_retries_unusable_json(self):
        good = json.dumps({"update_index": "# Index\n", "append_log": "ok"})
        backend = Replies("not json", '{"append_log": "missing index"}', good)
        run_maintainer(backend, self.ws, 1, {}, [])
        self.assertEqual(len(backend.prompts), 3)
        self.assertIn("previous reply was unusable", backend.prompts[1])
        with self.assertRaises(RuntimeError):
            run_maintainer(Replies("x", "y", "z"), self.ws, 1, {}, [])


class ProposerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(Path(self.tmp.name) / "ws")
        self.ws.init()
        self.tasks = [Task(f"t{i}", "train", "q", answer="A|B") for i in range(1, 6)]
        self.outcomes = {t.id: Outcome(t.id, 0.0, False, "wrong\nline") for t in self.tasks}
        self.ws.write_raw(1, {t.id: f"trace {t.id}" for t in self.tasks}, {})
        (self.ws.runs / "baseline_val").mkdir(parents=True)
        (self.ws.runs / "baseline_val" / "secret.md").write_text("val data", encoding="utf-8")
        self.suite = Suite("s", self.ws.root, "sales analysis", "{skill_section}", {"type": "exact"}, 1.0, False,
                           self.ws.root / "t.jsonl", self.ws.root, tuple(self.tasks))

    def test_sandbox(self):
        box = ReadSandbox(self.ws, 1)
        self.assertEqual(box.read("traces/t1"), "trace t1")
        self.assertEqual(box.read("traces/t2.md"), "trace t2")
        self.assertIn("Index", box.read("wiki/index.md"))
        self.assertIn("patterns", box.read("wiki"))
        for bad in ("runs/baseline_val/secret.md", "wiki/../runs/baseline_val/secret.md",
                    "traces/../../../runs/baseline_val/secret.md", "/etc/passwd", "raw/iter_001/summary.json"):
            self.assertTrue(box.read(bad).startswith("ERROR"), bad)
        box.read("traces/T1")  # same file on case-insensitive systems: still one trace
        self.assertEqual(box.traces_read, {"t1", "t2"})

    def test_summary_escapes_cells(self):
        table = outcome_summary(self.tasks, self.outcomes)
        self.assertIn("A\\|B", table)
        self.assertIn("wrong line", table)

    def turn(self, tool, **kw):
        return json.dumps({"thought": "", "tool": tool, **kw})

    def test_finish_requires_four_traces_and_a_valid_skill(self):
        create = {"action": "create", "name": "report-total", "skill_md": GOOD_SKILL, "purpose_md": "p"}
        bad = dict(create, name="Bad Name")
        replies = [self.turn("finish", proposal=create),  # too early: no traces read
                   *[self.turn("read_file", path=f"traces/t{i}") for i in range(1, 5)],
                   self.turn("finish", proposal=bad),  # invalid name -> error observation
                   self.turn("finish", proposal=create)]
        backend = Replies(*replies)
        proposal = run_proposer(backend, self.ws, self.suite, 1, self.tasks, self.outcomes)
        self.assertEqual(proposal["name"], "report-total")
        self.assertIn("read at least 4 execution traces", backend.prompts[1])
        self.assertIn("invalid skill name", backend.prompts[6])
        transcript = (self.ws.runs / "iter_001" / "proposer_transcript.md").read_text(encoding="utf-8")
        self.assertIn("## Turn 7", transcript)

    def test_no_action_and_turn_limit(self):
        backend = Replies(self.turn("finish", proposal={"action": "no_action"}))
        self.assertEqual(run_proposer(backend, self.ws, self.suite, 1, self.tasks, self.outcomes),
                         {"action": "no_action"})
        backend = Replies(*["garbage"] * 3)
        result = run_proposer(backend, self.ws, self.suite, 1, self.tasks, self.outcomes, max_turns=3)
        self.assertEqual(result["action"], "no_action")
        self.assertIn("last turn", backend.prompts[2])


if __name__ == "__main__":
    unittest.main()
