import os
import tempfile
import unittest
from pathlib import Path

from wikiskill.backends import AgentResult
from wikiskill.config import Suite, Task
from wikiskill.rollout import RolloutInfraError, format_trace, run_task, run_tasks, system_prompt
from wikiskill.scoring import Outcome


class ScriptedAgent:
    """Backend stand-in: `behaviour(cwd)` returns an AgentResult."""

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def run_agent(self, prompt, system, cwd):
        self.calls.append({"prompt": prompt, "system": system, "cwd": Path(cwd)})
        return self.behaviour(Path(cwd))


def make_suite(root: Path) -> Suite:
    files = root / "files"
    files.mkdir(parents=True, exist_ok=True)
    (files / "sales.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return Suite("s", root, "desc", "Solve.\n{skill_section}", {"type": "exact"}, 1.0, False,
                 root / "t.jsonl", files, ())


def ok_agent(cwd: Path) -> AgentResult:
    (cwd / "out.txt").write_text("done", encoding="utf-8")
    return AgentResult("thinking <answer>3</answer>", "trace text", ok=True)


class RolloutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.suite = make_suite(self.root)
        self.task = Task("t1", "train", "Sum column b.", answer="3", files=("sales.csv",))

    def test_runs_in_temp_dir_outside_workspace_and_caches(self):
        agent = ScriptedAgent(ok_agent)
        out = self.root / "ws" / "runs" / "stage" / "t1"
        result = run_task(agent, self.suite, self.task, "SYS", out)
        cwd = agent.calls[0]["cwd"]
        self.assertNotIn(self.root / "ws", cwd.parents)
        self.assertFalse(cwd.exists())  # temp dir removed after the run
        self.assertIn("sales.csv", agent.calls[0]["prompt"])
        self.assertEqual(result.prediction, "3")
        self.assertEqual(result.files, ("out.txt", "sales.csv"))
        self.assertTrue((out / "work" / "out.txt").is_file())
        again = run_task(agent, self.suite, self.task, "SYS", out)
        self.assertEqual(len(agent.calls), 1)  # second call served from the cache
        self.assertEqual(again, result)

    def test_infra_failures_are_not_cached_and_stop_the_stage(self):
        agent = ScriptedAgent(lambda cwd: AgentResult("", "", ok=False, error="timed out", infra_error=True))
        stage = self.root / "stage"
        with self.assertRaises(RolloutInfraError):
            run_tasks(agent, self.suite, [self.task], [], stage, workers=2)
        self.assertFalse((stage / "t1" / "result.json").exists())
        retry = ScriptedAgent(ok_agent)
        results = run_tasks(retry, self.suite, [self.task], [], stage, workers=2)
        self.assertTrue(results["t1"].ok)

    def test_agent_failure_is_cached_and_scored(self):
        agent = ScriptedAgent(lambda cwd: AgentResult("", "", ok=False, error="gave up"))
        results = run_tasks(agent, self.suite, [self.task], [], self.root / "stage", workers=1)
        self.assertFalse(results["t1"].ok)
        self.assertTrue((self.root / "stage" / "t1" / "result.json").exists())

    def test_cache_depends_on_model_and_input_files(self):
        out = self.root / "o"
        agent = ScriptedAgent(ok_agent)
        agent.name = "claude:haiku"
        run_task(agent, self.suite, self.task, "SYS", out)
        agent.name = "claude:sonnet"  # different model: run again
        run_task(agent, self.suite, self.task, "SYS", out)
        (self.suite.files_dir / "sales.csv").write_text("a,b\n9,9\n", encoding="utf-8")  # input changed
        run_task(agent, self.suite, self.task, "SYS", out)
        run_task(agent, self.suite, self.task, "SYS", out)  # unchanged: cached
        self.assertEqual(len(agent.calls), 3)

    def test_interrupt_stops_running_clis(self):
        from unittest import mock

        def interrupted(cwd):
            raise KeyboardInterrupt

        with mock.patch("wikiskill.rollout.kill_all_running") as kill_all, self.assertRaises(KeyboardInterrupt):
            run_tasks(ScriptedAgent(interrupted), self.suite, [self.task], [], self.root / "stage", workers=2)
        kill_all.assert_called_once()

    @unittest.skipIf(os.name == "nt", "creating symlinks needs extra rights on Windows")
    def test_dangling_symlink_does_not_abort(self):
        def agent_with_link(cwd: Path) -> AgentResult:
            os.symlink(str(cwd / "nowhere"), str(cwd / "link"))
            return ok_agent(cwd)
        result = run_task(ScriptedAgent(agent_with_link), self.suite, self.task, "SYS", self.root / "o")
        self.assertTrue(result.ok)

    def test_system_prompt_and_trace_format(self):
        self.assertEqual(system_prompt(self.suite, []), "Solve.\n")
        self.assertIn('<skill name="x">', system_prompt(self.suite, [("x", "---\nname: x\n---\n")]))
        result = run_task(ScriptedAgent(ok_agent), self.suite, self.task, "SYS", self.root / "o2")
        text = format_trace(self.task, result, Outcome("t1", 1.0, True, "3"))
        self.assertIn("Outcome: PASS", text)
        self.assertIn("Expected answer: 3", text)
        self.assertIn("trace text", text)


if __name__ == "__main__":
    unittest.main()
