"""The backend on top of agent_cli: version detection, pinned config arguments, failures that stop a run.

A scripted stand-in for `claude` / `codex` answers `--version`, rejects chosen arguments like the real
CLIs do, and prints one planned output per model call (the outputs are the measured ones of test_agent_cli).
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_agent_cli import CLAUDE_AUTH_RESULT, CODEX_LIMIT_STREAM, lines
from wikiskill import backends, cli
from wikiskill.backends import BackendError, Budget, BudgetExceeded, UsageLimitReached, make_backend
from wikiskill.config import Suite, Task
from wikiskill.rollout import run_tasks

SCRIPT = r'''
import json, os, sys
with open(os.environ["SCRIPTED_CLI_PLAN"], encoding="utf-8") as f:
    plan = json.load(f)
args = sys.argv[1:]
sys.stdin.buffer.read()
with open(plan["record"], encoding="utf-8") as f:
    calls = sum(1 for line in f if json.loads(line) != ["--version"])
with open(plan["record"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print(plan.get("version", "no version here"))
    sys.exit(0)
for flag in plan.get("reject", []):
    if flag in args:
        sys.stderr.write("error: unknown option '%s'\n" % flag)
        sys.exit(1)
out = plan["outputs"][min(calls, len(plan["outputs"]) - 1)]
sys.stdout.buffer.write(out.get("stdout", "").encode("utf-8"))
sys.stderr.write(out.get("stderr", ""))
sys.exit(out.get("exit", 0))
'''
OK_RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "total_cost_usd": 0.25}
OK = {"stdout": json.dumps(OK_RESULT)}
AUTH = {"stdout": json.dumps(CLAUDE_AUTH_RESULT), "exit": 1}
CODEX_OK = {"stdout": lines([{"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                             {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}])}
NEW_CLAUDE, OLD_CLAUDE = "2.1.325 (Claude Code)", "2.1.100 (Claude Code)"
# the example of docs/CONFIGURATION.md, section 3, as users of 0.11.0 wrote it
DOC_AGENT_ARGS = ["-p", "--safe-mode", "--output-format", "stream-json", "--verbose",
                  "--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Write,Edit",
                  "--disallowedTools", "WebFetch,WebSearch", "--no-session-persistence"]


class ScriptedCLITestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "bin").mkdir()
        script = self.root / "scripted_cli.py"
        script.write_text(SCRIPT, encoding="utf-8")
        for name in ("claude", "codex"):
            if os.name == "nt":  # a .cmd shim, like npm installs
                (self.root / "bin" / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
            else:
                shim = self.root / "bin" / name
                shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
                shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.record = self.root / "record.jsonl"
        self.record.write_text("", encoding="utf-8")
        self.workdir = self.root / "work dir"
        self.workdir.mkdir()
        env = {"PATH": str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
               "SCRIPTED_CLI_PLAN": str(self.root / "plan.json"), "HOME": str(self.root / "home"),
               "USERPROFILE": str(self.root / "home")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(backends.reset_stop)

    def plan(self, *outputs, version=NEW_CLAUDE, reject=()):
        plan = {"record": str(self.record), "outputs": list(outputs), "version": version, "reject": list(reject)}
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def calls(self, versions=False):
        """The argument lists of the model calls so far (`versions`: of the --version calls)."""
        every = [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]
        return [args for args in every if (args == ["--version"]) == versions]

    def backend(self, kind="claude", budget=None, **overrides):
        path = self.root / "backend.json"
        path.write_text(json.dumps({kind: {"retry_wait": 0, **overrides}}), encoding="utf-8")
        return make_backend(kind, config_path=path, budget=budget)


class DetectTest(ScriptedCLITestCase):
    def test_the_version_is_read_once_and_the_agent_arguments_follow_it(self):
        self.plan(OK)
        backend = self.backend()
        self.assertIsNone(backend.cli.version)
        self.assertEqual(backend.detect().version, (2, 1, 325))
        backend.detect()
        backend.run_agent("task", "skills", self.workdir)
        self.assertEqual(len(self.calls(versions=True)), 1)
        args = self.calls()[0]
        denied = args[args.index("--disallowedTools") + 1]
        self.assertTrue(denied.startswith("WebFetch,WebSearch,"))
        self.assertIn("CronCreate", denied)
        self.assertIn("--append-system-prompt-file", args)
        self.assertNotIn("--max-budget-usd", args)  # no budget, no cap

    def test_without_detect_the_arguments_are_those_of_0_11(self):
        self.plan(OK)
        self.backend().run_agent("task", None, self.workdir)
        self.assertEqual(self.calls(), [list(backends.DEFAULTS["claude"]["agent_args"])])
        self.assertEqual(self.calls(versions=True), [])

    def test_an_unreadable_version_changes_nothing(self):
        for version in ("Python 3.13.5", ""):
            self.record.write_text("", encoding="utf-8")
            self.plan(OK, version=version)
            backend = self.backend()
            self.assertIsNone(backend.detect().version)
            backend.complete("x")
            self.assertEqual(self.calls(), [list(backends.DEFAULTS["claude"]["complete_args"])])
        missing = self.backend(command="no-such-cli")
        self.assertIsNone(missing.detect().version)  # nothing to ask; the call itself reports the missing command

    def test_a_budgeted_call_may_spend_only_what_is_left_today(self):
        self.plan(OK)
        budget = Budget(self.root / "spend.json", 1.0)
        budget.add(0.25)
        backend = self.backend(budget=budget)
        backend.complete("x")  # version unknown: the cap needs a CLI known to have it
        backend.detect()
        backend.complete("x")
        first, second = self.calls()
        self.assertNotIn("--max-budget-usd", first)
        self.assertEqual(second[-2:], ["--max-budget-usd", "0.50"])  # 1.00 - 0.25 - the first call's 0.25
        shared = Budget(self.root / "total.json", 0.6)
        capped = self.backend(budget=Budget(self.root / "other.json", 5.0, shared))
        capped.detect()
        capped.complete("x")
        self.assertEqual(self.calls()[-1][-2:], ["--max-budget-usd", "0.60"])  # the nearer limit counts

    def test_check_shows_the_version_and_the_arguments(self):
        self.plan(OK)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["check", "--no-call"])
        self.assertEqual(code, 0)
        self.assertIn("version: claude 2.1.325", out.getvalue())
        self.assertIn("agent arguments (chosen for this version)", out.getvalue())
        self.assertEqual(self.calls(), [])  # --no-call: the model is not called


class PinnedConfigTest(ScriptedCLITestCase):
    def test_arguments_written_in_a_config_file_are_used_as_written(self):
        self.plan(OK)
        budget = Budget(self.root / "spend.json", 1.0)
        backend = self.backend(budget=budget, agent_args=DOC_AGENT_ARGS, timeout=3600)
        backend.detect()
        backend.run_agent("task", "skills", self.workdir)
        args = self.calls()[0]
        self.assertEqual(args[:len(DOC_AGENT_ARGS)], DOC_AGENT_ARGS)
        self.assertEqual(args[len(DOC_AGENT_ARGS)], "--append-system-prompt-file")
        self.assertEqual(len(args), len(DOC_AGENT_ARGS) + 2)  # nothing else: no cap, no extra denied tools
        self.assertEqual(backend.config.timeout, 3600)
        backend.complete("x")  # the mode the file did not write is still chosen for the version
        self.assertIn("--max-budget-usd", self.calls()[1])
        shown = "\n".join(backend.describe())
        self.assertIn("agent arguments (set by the backend config, used as written): -p --safe-mode", shown)
        self.assertIn("complete arguments (chosen for this version)", shown)

    def test_the_other_config_keys_do_not_pin(self):
        self.plan(OK)
        backend = self.backend(command="claude", retries=1, timeout=60, model_flag="--model")
        self.assertEqual(backend.config.pinned, frozenset())
        with self.assertRaises(backends.ConfigError):  # not a key of the file format
            self.backend(pinned=["agent"])

    def test_a_rejected_pinned_argument_names_the_config_file(self):
        self.plan(OK, reject=["--safe-mode"], version=OLD_CLAUDE)
        backend = self.backend(complete_args=list(backends.DEFAULTS["claude"]["complete_args"]), retries=2)
        with self.assertRaises(BackendError) as caught:
            backend.complete("x")
        self.assertEqual(caught.exception.kind, "bad_option")
        self.assertIn("unknown option '--safe-mode'", str(caught.exception))
        self.assertIn("backend config", str(caught.exception))
        self.assertEqual(len(self.calls()), 1)  # not retried: the same arguments would be rejected again


class FailureKindTest(ScriptedCLITestCase):
    def test_a_rejected_argument_is_tried_again_with_the_arguments_of_the_real_version(self):
        self.plan(OK, reject=["--safe-mode"], version=OLD_CLAUDE)
        backend = self.backend(retries=0)  # nobody called detect(), as a library user might
        self.assertEqual(backend.complete("x"), "OK")
        first, second = self.calls()
        self.assertIn("--safe-mode", first)
        self.assertNotIn("--safe-mode", second)
        self.assertEqual(len(self.calls(versions=True)), 1)
        self.assertEqual(backend.cli.version, (2, 1, 100))

    def test_an_argument_the_detected_version_rejects_stops_with_advice(self):
        self.plan(OK, reject=["--no-session-persistence"])
        backend = self.backend(retries=2)
        backend.detect()
        with self.assertRaises(BackendError) as caught:
            backend.complete("x")
        self.assertTrue(caught.exception.fatal)
        self.assertIn("docs/CONFIGURATION.md", str(caught.exception))
        self.assertEqual(len(self.calls()), 1)

    def test_not_logged_in_stops_at_once_and_says_what_to_do(self):
        self.plan(AUTH)
        backend = self.backend(retries=2, retry_wait=30)
        with self.assertRaises(BackendError) as caught:
            backend.complete("x")
        self.assertEqual((caught.exception.kind, caught.exception.fatal), ("auth", True))
        self.assertIn("Not logged in", str(caught.exception))
        self.assertIn("log in", str(caught.exception))
        with self.assertRaises(BackendError):  # not an AgentResult: no other task could do better
            backend.run_agent("task", None, self.workdir)
        self.assertEqual(len(self.calls()), 2)  # one call each, no retries, no 30 s waits

    def test_a_rollout_stops_at_the_first_login_failure(self):
        self.plan(AUTH)
        (self.root / "files").mkdir()
        suite = Suite("s", self.root, "d", "{skill_section}", {"type": "exact"}, 1.0, False, self.root / "t",
                      self.root / "files", ())
        tasks = [Task(f"t{i}", "train", "q", answer="1") for i in range(5)]
        with self.assertRaises(BackendError) as caught:
            run_tasks(self.backend(retries=2), suite, tasks, [], self.root / "stage", workers=1)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(len(self.calls()), 1)  # 0.11.0 made 15 calls here and reported an outage

    def test_a_usage_limit_stops_like_a_spent_budget_and_is_not_counted(self):
        self.plan({"stdout": lines(CODEX_LIMIT_STREAM), "exit": 1}, version="codex-cli 0.169.0")
        budget = Budget(self.root / "spend.json", 1.0)
        budget.start_run()
        backend = self.backend("codex", budget=budget, retries=2, retry_wait=30)
        with self.assertRaises(UsageLimitReached) as caught:
            backend.complete("x")
        self.assertIsInstance(caught.exception, BudgetExceeded)  # `improve` pauses and resumes later
        self.assertIn("try again at 4:26 AM", str(caught.exception))
        with self.assertRaises(UsageLimitReached):
            backend.run_agent("task", None, self.workdir)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(budget.today()["unpriced_calls"], 0)  # the model did no work
        self.assertIsNone(budget.blocked())  # so tomorrow's one run a day is not used up either

    def test_a_usage_limit_on_stderr_alone_is_recognised_too(self):
        self.plan({"stderr": "Claude AI usage limit reached\n", "exit": 1})
        with self.assertRaises(UsageLimitReached):
            self.backend(retries=2, retry_wait=30).complete("x")
        self.assertEqual(len(self.calls()), 1)

    def test_a_short_rate_limit_is_retried(self):
        busy = dict(CLAUDE_AUTH_RESULT, result="API Error: 429 Too many requests", api_error_status=429)
        self.plan({"stdout": json.dumps(busy), "exit": 1}, OK)
        self.assertEqual(self.backend(retries=1).complete("x"), "OK")
        self.assertEqual(len(self.calls()), 2)
        self.record.write_text("", encoding="utf-8")
        self.plan({"stdout": json.dumps(busy), "exit": 1})
        with self.assertRaises(BackendError) as caught:
            self.backend(retries=1).complete("x")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(len(self.calls()), 2)

    def test_reaching_the_cost_cap_inside_one_call_is_a_spent_budget(self):
        capped = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 0.75}
        self.plan({"stdout": json.dumps(capped), "exit": 1})
        budget = Budget(self.root / "spend.json", 1.0)
        backend = self.backend(budget=budget, retries=2)
        backend.detect()
        with self.assertRaises(BudgetExceeded):
            backend.run_agent("task", None, self.workdir)
        self.assertEqual(budget.today()["usd"], 0.75)  # what the cut-off call cost is counted
        self.assertEqual(len(self.calls()), 1)

    def test_codex_usage_is_parsed_and_a_finished_call_counts_as_unpriced(self):
        self.plan(CODEX_OK, version="codex-cli 0.169.0")
        budget = Budget(self.root / "spend.json", 1.0)
        backend = self.backend("codex", budget=budget)
        self.assertEqual(backend.detect().version, (0, 169, 0))
        self.assertEqual(backend.complete("x", system="SYS"), "OK")
        self.assertEqual(self.calls()[0][:8], list(backends.DEFAULTS["codex"]["complete_args"]))
        self.assertEqual(budget.today()["unpriced_calls"], 1)


if __name__ == "__main__":
    unittest.main()
