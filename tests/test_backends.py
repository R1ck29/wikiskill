import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from wikiskill.backends import BackendError, load_backend_config, make_backend
from wikiskill.config import ConfigError

FAKE_CLI = Path(__file__).with_name("fake_cli.py")
JAPANESE = "売上の合計を計算してください。「税込」も考慮。"


def install_fake_cli(bin_dir: Path, name: str) -> None:
    """Create `name` on PATH that runs fake_cli.py (a .cmd shim on Windows, like npm installs)."""
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{FAKE_CLI}" %*\r\n', encoding="utf-8")
    else:
        script = bin_dir / name
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)


class FakeCLITestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "bin").mkdir()
        for name in ("claude", "codex"):
            install_fake_cli(root / "bin", name)
        self.record = root / "record.jsonl"
        self.workdir = root / "work dir"  # a space in the path on purpose
        self.workdir.mkdir()
        env = {"PATH": str(root / "bin") + os.pathsep + os.environ.get("PATH", ""),
               "FAKE_CLI_RECORD": str(self.record), "FAKE_CLI_REPLY": "答え: <answer>1,234</answer>"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self):
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]


class ClaudeBackendTest(FakeCLITestCase):
    def test_complete_passes_prompt_on_stdin_and_system_via_file(self):
        backend = make_backend("claude", model="sonnet")
        self.assertEqual(backend.complete(JAPANESE, system="SYS " + JAPANESE), "答え: <answer>1,234</answer>")
        call = self.calls()[0]
        self.assertEqual(call["stdin"], JAPANESE)
        args = call["args"]
        self.assertIn("--system-prompt-file", args)
        self.assertEqual(args[args.index("--tools") + 1], "")  # empty arg survives shims
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertNotIn(JAPANESE, " ".join(args))  # content never goes on the command line

    def test_run_agent_renders_trace_and_runs_in_cwd(self):
        result = make_backend("claude").run_agent(JAPANESE, "skills here", self.workdir)
        self.assertTrue(result.ok)
        self.assertEqual(result.final_text, "答え: <answer>1,234</answer>")
        self.assertIn("tool call `Bash`", result.trace)
        self.assertIn("data.csv", result.trace)
        call = self.calls()[0]
        self.assertEqual(Path(call["cwd"]).resolve(), self.workdir.resolve())
        self.assertIn("--append-system-prompt-file", call["args"])
        self.assertIn("WebFetch,WebSearch", call["args"])


class CodexBackendTest(FakeCLITestCase):
    def test_complete_prepends_system_and_uses_stdin(self):
        out = make_backend("codex").complete(JAPANESE, system="SYSTEM")
        self.assertEqual(out, "答え: <answer>1,234</answer>")
        call = self.calls()[0]
        self.assertTrue(call["stdin"].startswith("SYSTEM"))
        self.assertTrue(call["stdin"].endswith(JAPANESE))
        self.assertEqual(call["args"][-1], "-")
        self.assertIn("web_search=disabled", call["args"])
        self.assertIn("read-only", call["args"])

    def test_run_agent(self):
        result = make_backend("codex", model="gpt-5.5").run_agent("q", None, self.workdir)
        self.assertTrue(result.ok)
        self.assertIn("tool call `shell`", result.trace)
        args = self.calls()[0]["args"]
        self.assertEqual(Path(args[args.index("-C") + 1]).resolve(), self.workdir.resolve())
        self.assertIn("workspace-write", args)


class FailureTest(FakeCLITestCase):
    def test_missing_command_and_nonzero_exit(self):
        config_path = Path(self.tmp.name) / "backend.json"
        config_path.write_text(json.dumps({"claude": {"command": "no-such-cli", "retries": 0}}), encoding="utf-8")
        backend = make_backend("claude", config_path=config_path)
        with self.assertRaises(BackendError):
            backend.complete("x")
        self.assertFalse(backend.run_agent("x", None, self.workdir).ok)

    def backend_with(self, kind, **overrides):
        config_path = Path(self.tmp.name) / "backend.json"
        config_path.write_text(json.dumps({kind: overrides}), encoding="utf-8")
        return make_backend(kind, config_path=config_path)

    def test_crash_is_retried_then_reported_as_infra_error(self):
        backend = self.backend_with("codex", retries=2, retry_wait=0)
        with mock.patch.dict(os.environ, {"FAKE_CLI_SILENT": "1", "FAKE_CLI_EXIT": "1"}):
            result = backend.run_agent("x", None, self.workdir)
            with self.assertRaises(BackendError):
                backend.complete("x")
        self.assertFalse(result.ok)
        self.assertTrue(result.infra_error)
        self.assertEqual(len(self.calls()), 6)  # 3 attempts each

    def test_empty_codex_message_is_a_normal_result(self):
        with mock.patch.dict(os.environ, {"FAKE_CLI_REPLY": "__EMPTY__"}):  # Windows drops empty env vars
            result = make_backend("codex").run_agent("x", None, self.workdir)
        self.assertTrue(result.ok)
        self.assertEqual(result.final_text, "")

    def test_claude_api_error_is_retried(self):
        backend = self.backend_with("claude", retries=1, retry_wait=0)
        bad = '{"type":"result","is_error":true,"result":"API Error: 429 Too many requests"}\n'
        with self.assertRaises(BackendError):
            backend._parse_agent(bad)
        with self.assertRaises(BackendError):
            backend._parse_agent('{"type":"turn.failed","error":"x"}\n')
        codex = make_backend("codex")
        with self.assertRaises(BackendError):
            codex._parse_agent('{"type":"turn.failed","error":"plain string"}\n')

    def test_timeout_kills_process_tree(self):
        backend = self.backend_with("claude", timeout=2, retries=0)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"FAKE_CLI_HANG": "1"}):
            result = backend.run_agent("x", None, self.workdir)
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(result.infra_error)
        self.assertIn("timed out", result.error)

    def test_ctrl_c_during_parallel_rollouts_stops_everything(self):
        """Reviewer scenario: retries must not restart CLIs after Ctrl+C, and no process may survive."""
        import _thread
        import threading

        from wikiskill import backends
        from wikiskill.config import Suite, Task
        from wikiskill.rollout import run_tasks
        backend = self.backend_with("claude", retries=2, retry_wait=10, timeout=120)
        self.addCleanup(backends.reset_stop)  # the stop flag is process-wide; later tests need it cleared
        root = Path(self.tmp.name)
        (root / "files").mkdir()
        suite = Suite("s", root, "d", "{skill_section}", {"type": "exact"}, 1.0, False, root / "t", root / "files", ())
        tasks = [Task(f"t{i}", "train", "q", answer="1") for i in range(3)]
        threading.Timer(2.0, _thread.interrupt_main).start()  # what a terminal Ctrl+C does
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"FAKE_CLI_HANG": "1"}), self.assertRaises(KeyboardInterrupt):
            run_tasks(backend, suite, tasks, [], root / "stage", workers=3)
        self.assertLess(time.monotonic() - started, 30)  # no 10 s retry waits, no 60 s hangs
        self.assertEqual(len(self.calls()), 3)  # each task started once; nothing was restarted
        self.assertEqual(backends._LIVE, set())  # every CLI was stopped before returning

    def test_interrupt_kills_process_tree(self):
        from wikiskill import backends
        killed = []
        real_kill = backends._kill_tree

        real_communicate = backends.subprocess.Popen.communicate

        def interrupting_communicate(self, *a, **kw):
            if self.args[0] == sys.executable:  # only the CLI under test, not taskkill on Windows
                raise KeyboardInterrupt
            return real_communicate(self, *a, **kw)

        with mock.patch.object(backends.subprocess.Popen, "communicate", interrupting_communicate), \
                mock.patch.object(backends, "_kill_tree", lambda proc: (killed.append(proc.pid), real_kill(proc))), \
                self.assertRaises(KeyboardInterrupt):
            backends.run_process([sys.executable, "-c", "import time; time.sleep(60)"], "", self.workdir, 30)
        self.assertEqual(len(killed), 1)

    def test_unknown_config_keys_rejected(self):
        config_path = Path(self.tmp.name) / "backend.json"
        config_path.write_text(json.dumps({"claude": {"timeoutt": 5}}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_backend_config("claude", config_path=config_path)
        with self.assertRaises(ConfigError):
            load_backend_config("gemini")
        for bad_model in ("sonnet & del *", "--dangerously-skip-permissions"):
            with self.assertRaises(ConfigError):
                load_backend_config("claude", model=bad_model)
        self.assertEqual(load_backend_config("claude", model="arn:aws:bedrock:us-east-1:1:x/y").model,
                         "arn:aws:bedrock:us-east-1:1:x/y")


if __name__ == "__main__":
    unittest.main()
