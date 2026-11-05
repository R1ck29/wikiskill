import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeBackend
from tests.test_evolve import write_suite
from wikiskill import cli
from wikiskill.backends import AgentResult
from wikiskill.cli import EXIT_CONFIG, EXIT_REVIEW, main
from wikiskill.config import load_suite
from wikiskill.evolve import Evolution, EvolveOptions
from wikiskill.skills import EXPORT_MARK
from wikiskill.util import read_text
from wikiskill.workspace import Workspace


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = main([str(a) for a in argv])
    return code, out.getvalue()


class CLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.suite_dir = write_suite(self.root / "suite")
        self.ws = Workspace(self.root / "ws")
        Evolution(load_suite(self.suite_dir), FakeBackend(), self.ws, EvolveOptions(iterations=3, workers=1)).run()

    def test_status_and_export(self):
        code, out = run_cli("status", self.ws.root)
        self.assertEqual(code, 0)
        self.assertIn("accepted", out)
        self.assertIn("report-total", out)
        dest = self.root / "project" / ".claude" / "skills"
        code, out = run_cli("export", self.ws.root, "--dest", dest)
        self.assertEqual(code, 0)
        self.assertIn(EXPORT_MARK, read_text(dest / "report-total" / "SKILL.md"))
        self.assertFalse((dest / "report-total" / "PURPOSE.md").exists())
        code, out = run_cli("export", self.ws.root, "--dest", dest)
        self.assertIn("skip report-total", out)
        code, out = run_cli("export", self.ws.root, "--dest", dest, "--force", "--suite", self.suite_dir)
        self.assertEqual(code, 0, out)
        exported = read_text(dest / "report-total" / "SKILL.md")
        self.assertIn("## Feedback for automatic improvement", exported)
        self.assertIn(str(self.suite_dir.resolve()), exported)
        self.assertEqual(run_cli("export", self.ws.root, "--dest", dest, "--suite", self.root / "missing")[0],
                         EXIT_CONFIG)
        with mock.patch.dict("os.environ", {"HOME": str(self.root), "USERPROFILE": str(self.root)}):
            run_cli("export", self.ws.root, "--dest", "~/.agents/skills")
        self.assertTrue((self.root / ".agents" / "skills" / "report-total" / "SKILL.md").is_file())

    def test_compare(self):
        for name, scores in (("a", {"x1": 0, "x2": 0}), ("b", {"x1": 1, "x2": 1})):
            (self.root / f"{name}.json").write_text(json.dumps({"scores": scores}), encoding="utf-8")
        code, out = run_cli("compare", self.root / "a.json", self.root / "b.json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["difference_b_minus_a"], 1.0)

    def test_check_fails_when_the_agent_cannot_use_tools(self):
        class Backend:
            name, config = "stub", argparse.Namespace(command=sys.executable)

            def __init__(self, writes):
                self.writes = writes

            def complete(self, prompt, system=None):
                return "OK"

            def run_agent(self, prompt, system, cwd):
                if self.writes:
                    (cwd / "sum.txt").write_text("42\n", encoding="utf-8")
                return AgentResult("<answer>42</answer>", "", ok=True)

        for writes, expected in ((True, 0), (False, 1)):
            with mock.patch.object(cli, "make_backend", lambda *a, w=writes: Backend(w)):
                code, out = run_cli("check")
            self.assertEqual(code, expected, out)
        self.assertIn("bypassPermissions", out)

    def test_check_suite_without_calls_and_config_errors(self):
        code, out = run_cli("check", "--suite", self.suite_dir, "--no-call", "--backend-config",
                            self._config({"claude": {"command": "definitely-not-installed"}}))
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("NOT FOUND", out)
        self.assertIn("'train': 4", out)
        code, out = run_cli("check", "--suite", self.root / "missing", "--no-call")
        self.assertEqual(code, EXIT_CONFIG)
        code, out = run_cli("status", self.root / "nothing")
        self.assertEqual(code, EXIT_CONFIG)

    def test_human_review_exit_code(self):
        code, out = run_cli("evolve", self.suite_dir, "--workspace", self.root / "ws2", "--human-review", "on",
                            "--backend-config", self._config({"claude": {"command": "definitely-not-installed",
                                                                          "retries": 0}}))
        self.assertEqual(code, 1)  # CLI missing -> every rollout is an infra failure -> stage stops
        self.assertIn("run the same command again", out)

    def test_human_review_pause_returns_exit_3(self):
        with mock.patch("wikiskill.cli.make_backend", lambda *a, **k: FakeBackend()):
            code, out = run_cli("evolve", self.suite_dir, "--workspace", self.root / "ws3", "--human-review", "on")
        self.assertEqual(code, EXIT_REVIEW)
        self.assertIn("baseline_val.csv", out)

    def test_a_judge_on_the_same_model_shares_the_backend(self):
        with mock.patch("wikiskill.cli.make_backend", side_effect=lambda *a: object()) as make:
            for judge_model, calls in ((None, 1), ("m", 1), ("other", 2)):
                make.reset_mock()
                backend, judge = cli._backends(argparse.Namespace(backend="claude", model="m", judge_model=judge_model,
                                                                  backend_config=None))
                self.assertEqual(make.call_count, calls)
                self.assertEqual(backend is judge, calls == 1)

    def test_a_daily_budget_must_be_a_finite_amount(self):
        self.assertEqual(cli._usd("2.5"), 2.5)
        for bad in ("0", "-1", "inf", "nan"):
            with self.assertRaises(argparse.ArgumentTypeError):
                cli._usd(bad)

    def test_user_errors_are_reported_not_raised(self):
        code, out = run_cli("compare", self.root / "missing.json", self.root / "missing.json")
        self.assertEqual(code, EXIT_CONFIG)
        code, out = run_cli("evaluate", self.suite_dir, "--workspace", self.root / "nope", "--out", self.root / "e")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("skills folder not found", out)

    def _config(self, data):
        path = self.root / "backend.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
