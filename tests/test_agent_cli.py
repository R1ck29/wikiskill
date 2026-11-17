"""agent_cli: versions, arguments, output parsing and failure kinds (text in, data out; no process).

The outputs marked "measured" were captured from the real CLIs (claude 2.1.325, codex-cli 0.169.0)
and shortened: see docs/design/REDESIGN.md, section 4.
"""
import ast
import json
import unittest
from pathlib import Path

from wikiskill import agent_cli
from wikiskill.agent_cli import CallSpec, classify, classify_exit, parse_output, parse_version

PACKAGE = Path(agent_cli.__file__).parent
# the argument lists of wikiskill 0.11.0, written out: an unknown CLI version must get exactly these
V0_11 = {
    ("claude", "complete"): ["-p", "--safe-mode", "--output-format", "json", "--tools", "",
                             "--no-session-persistence"],
    ("claude", "agent"): ["-p", "--safe-mode", "--output-format", "stream-json", "--verbose",
                          "--permission-mode", "bypassPermissions",
                          "--disallowedTools", "WebFetch,WebSearch", "--no-session-persistence"],
    ("codex", "complete"): ["exec", "--json", "--skip-git-repo-check", "--ephemeral",
                            "--sandbox", "read-only", "-c", "web_search=disabled"],
    ("codex", "agent"): ["exec", "--json", "--skip-git-repo-check", "--ephemeral",
                         "--sandbox", "workspace-write", "-c", "web_search=disabled"],
}
NOT_LOGGED_IN = "Not logged in · Please run /login"
CODEX_LIMIT = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
               "https://chatgpt.com/settings/usage to purchase more credits or try again at 4:26 AM.")
CLAUDE_LIMIT = "You've hit your 5-hour limit · resets 5:10am (Asia/Tokyo)"
# measured: claude 2.1.325 without a login, --output-format json (exit code 1)
CLAUDE_AUTH_RESULT = {
    "type": "result", "subtype": "success", "is_error": True, "result": NOT_LOGGED_IN, "total_cost_usd": 0,
    "terminal_reason": "api_error", "api_error_status": None, "num_turns": 1, "duration_ms": 79,
    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "iterations": []},
    "modelUsage": {}, "permission_denials": [], "fast_mode_state": "off", "result_index": 0,
}
# measured: the same failure as stream-json events
CLAUDE_AUTH_STREAM = [
    {"type": "system", "subtype": "ui_invalidate", "event": "ui.render"},
    {"type": "system", "subtype": "init", "claude_code_version": "2.1.325", "tools": ["Bash", "Read"],
     "permissionMode": "bypassPermissions", "apiKeySource": "none"},
    {"type": "assistant", "error": "authentication_failed", "is_api_error_message": True,
     "message": {"model": "<synthetic>", "content": [{"type": "text", "text": NOT_LOGGED_IN}]}},
    CLAUDE_AUTH_RESULT,
]
# measured: codex-cli 0.169.0 at its usage limit (exit code 1, empty stderr)
CODEX_LIMIT_STREAM = [
    {"type": "thread.started", "thread_id": "01a1e506"},
    {"type": "turn.started"},
    {"type": "error", "message": CODEX_LIMIT},
    {"type": "turn.failed", "error": {"message": CODEX_LIMIT}},
]


def lines(events):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


class VersionTest(unittest.TestCase):
    def test_reads_what_the_clis_print(self):
        self.assertEqual(parse_version("claude", "2.1.325 (Claude Code)\n").version, (2, 1, 325))
        self.assertEqual(parse_version("codex", "codex-cli 0.169.0\n").version, (0, 169, 0))
        self.assertEqual(parse_version("codex", "WARNING: something\ncodex-cli 0.146.1").version, (0, 146, 1))
        self.assertEqual(parse_version("claude", "2.1.325 (Claude Code)").label(), "claude 2.1.325")

    def test_anything_else_is_an_unknown_version_not_an_error(self):
        for kind, text in (("claude", "Python 3.13.5"), ("claude", "codex-cli 0.169.0"), ("codex", ""),
                           ("codex", "2.1.325 (Claude Code)"), ("claude", "error: unknown option '--version'")):
            info = parse_version(kind, text)
            self.assertIsNone(info.version, (kind, text))
            self.assertFalse(info.has("safe_mode"))
        self.assertIn("Python 3.13.5", parse_version("claude", "Python 3.13.5").label())
        with self.assertRaises(ValueError):
            parse_version("gemini", "1.0.0")


class ArgumentsTest(unittest.TestCase):
    def test_an_unknown_version_gets_the_arguments_of_0_11(self):
        for (kind, mode), expected in V0_11.items():
            self.assertEqual(list(agent_cli.base_args(kind, mode)), expected)
            self.assertEqual(list(agent_cli.base_args(kind, mode, agent_cli.unknown(kind))), expected)
            self.assertEqual(list(agent_cli.base_args(kind, mode, parse_version(kind, "garbage"))), expected)

    def test_a_known_claude_keeps_the_agent_inside_its_task(self):
        info = parse_version("claude", "2.1.325 (Claude Code)")
        args = agent_cli.base_args("claude", "agent", info)
        denied = args[args.index("--disallowedTools") + 1].split(",")
        self.assertEqual(denied[:2], ["WebFetch", "WebSearch"])
        for tool in ("CronCreate", "ScheduleWakeup", "PushNotification", "SendMessage"):
            self.assertIn(tool, denied)
        for tool in ("Bash", "Read", "Write", "Edit", "Task"):  # the tools a task is solved with stay
            self.assertNotIn(tool, denied)
        self.assertEqual(list(agent_cli.base_args("claude", "complete", info)), V0_11[("claude", "complete")])

    def test_a_claude_older_than_safe_mode_runs_without_it(self):
        old = parse_version("claude", "2.1.100 (Claude Code)")
        for mode in agent_cli.MODES:
            self.assertNotIn("--safe-mode", agent_cli.base_args("claude", mode, old))
            self.assertIn("--no-session-persistence", agent_cli.base_args("claude", mode, old))
        self.assertIn("--safe-mode", agent_cli.base_args("claude", "agent", parse_version("claude", "2.1.169 (Claude Code)")))

    def test_codex_arguments_do_not_depend_on_the_version(self):
        new = parse_version("codex", "codex-cli 0.169.0")
        for mode in agent_cli.MODES:
            args = agent_cli.base_args("codex", mode, new)
            self.assertEqual(list(args), V0_11[("codex", mode)])
            self.assertNotIn("--trust", args)  # would save trust for every temporary task folder
            self.assertNotIn("--ignore-user-config", args)  # the user's config may hold the model provider

    def test_call_arguments(self):
        spec = CallSpec("complete", model="sonnet", system_file="/tmp/s.md", cwd="/work dir", max_cost_usd=1.234)
        base = agent_cli.base_args("claude", "complete")
        self.assertEqual(agent_cli.call_args("claude", base, spec, model_flag="--model")[len(base):],
                         ["--model", "sonnet", "--system-prompt-file", "/tmp/s.md"])  # unknown version: no cap
        known = parse_version("claude", "2.1.325 (Claude Code)")
        capped = agent_cli.call_args("claude", base, spec, known)
        self.assertEqual(capped[-2:], ["--max-budget-usd", "1.23"])
        agent = agent_cli.call_args("claude", ["-p"], CallSpec("agent", system_file="/tmp/s.md", max_cost_usd=0.0), known)
        self.assertEqual(agent, ["-p", "--append-system-prompt-file", "/tmp/s.md", "--max-budget-usd", "0.01"])
        self.assertEqual(agent_cli.call_args("codex", ["exec"], spec, model_flag="-m"),
                         ["exec", "-m", "sonnet", "-C", "/work dir", "-"])
        self.assertEqual(agent_cli.join_prompt("codex", " SYS \n", "ask"), "SYS\n\n---\n\nask")
        self.assertEqual(agent_cli.join_prompt("claude", "SYS", "ask"), "ask")
        self.assertEqual(agent_cli.join_prompt("codex", None, "ask"), "ask")


class ParseClaudeTest(unittest.TestCase):
    def test_a_good_answer_with_usage(self):
        result = {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "total_cost_usd": 0.0123,
                  "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 300,
                            "cache_creation_input_tokens": 40}, "num_turns": 1,
                  "credential_expires_at": "2026-12-01T00:00:00Z", "a_field_from_the_future": {"x": 1}}
        out = parse_output("claude", "complete", json.dumps(result))
        self.assertEqual((out.text, out.error, out.finished), ("OK", None, True))
        self.assertEqual(out.usage, agent_cli.Usage(10, 2, 300, 40, 0.0123))
        self.assertEqual(out.meta["credential_expires_at"], "2026-12-01T00:00:00Z")

    def test_a_stream_becomes_text_trace_and_version(self):
        events = [
            {"type": "system", "subtype": "ui_invalidate"},
            {"type": "system", "subtype": "init", "claude_code_version": "2.1.325"},
            {"type": "event_from_the_future", "payload": [1, 2]},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "look first"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "block_from_the_future"}, "not a block"]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "a.csv"}]},
                                                      {"type": "tool_result", "content": "boom", "is_error": True}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "<answer>1</answer>"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "<answer>1</answer>",
             "total_cost_usd": 0.25},
        ]
        out = parse_output("claude", "agent", "a warning line\n" + lines(events))
        self.assertEqual((out.text, out.error, out.cli_version), ("<answer>1</answer>", None, "2.1.325"))
        self.assertEqual(out.usage.cost_usd, 0.25)
        for part in ("**reasoning:** look first", "**tool call `Bash`:**", '"command": "ls"', "**tool output:**",
                     "a.csv", "**tool error:**", "**assistant:** <answer>1</answer>"):
            self.assertIn(part, out.trace)

    def test_not_logged_in_is_an_auth_failure_in_both_formats(self):
        for mode, stdout in (("complete", json.dumps(CLAUDE_AUTH_RESULT)), ("agent", lines(CLAUDE_AUTH_STREAM))):
            out = parse_output("claude", mode, stdout)
            self.assertEqual((out.error.kind, out.error.message), ("auth", NOT_LOGGED_IN))
            self.assertTrue(out.error.fatal and not out.error.retryable and out.finished)
            self.assertEqual(out.usage.cost_usd, 0.0)  # a failed call reports its cost too

    def test_a_usage_limit_is_told_from_a_rate_limit_by_its_wording(self):
        limit = dict(CLAUDE_AUTH_RESULT, result=CLAUDE_LIMIT, api_error_status=429)
        events = [{"type": "assistant", "error": "rate_limit", "message": {"content": []}}, limit]
        self.assertEqual(parse_output("claude", "agent", lines(events)).error.kind, "usage_limit")
        busy = dict(CLAUDE_AUTH_RESULT, result="API Error: 429 Too many requests", api_error_status=429)
        error = parse_output("claude", "agent", lines([events[0], busy])).error
        self.assertEqual((error.kind, error.retryable, error.fatal), ("rate_limit", True, False))

    def test_the_cost_cap_of_one_call(self):
        capped = {"type": "result", "subtype": "error_max_budget_usd", "is_error": False, "total_cost_usd": 1.02}
        out = parse_output("claude", "agent", lines([capped]))
        self.assertEqual((out.error.kind, out.usage.cost_usd), ("cost_cap", 1.02))

    def test_cut_off_or_empty_output_is_incomplete_and_never_raises(self):
        for mode, stdout in (("agent", '{"type": "assistant"}\nplain text\n'), ("agent", ""), ("complete", ""),
                             ("complete", "Segmentation fault"), ("agent", '{"type": "result", "is_err'),
                             ("complete", "[1, 2]"), ("agent", '{"type": "assistant", "message": "x"}\n')):
            out = parse_output("claude", mode, stdout)
            self.assertEqual((out.error.kind, out.finished, out.usage), ("incomplete", False, None), stdout)
            self.assertTrue(out.error.retryable)
        self.assertIn("non-JSON", parse_output("claude", "complete", "Segmentation fault").error.message)
        # `--output-format json` without a string result is a failure, as before
        self.assertIsNotNone(parse_output("claude", "complete", '{"type": "result", "is_error": false}').error)
        self.assertEqual(parse_output("claude", "complete", '{"result": "plain", "is_error": false}').text, "plain")


class ParseCodexTest(unittest.TestCase):
    def test_a_turn_with_usage(self):
        events = [
            {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "think"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls",
                                                "aggregated_output": "a.csv\n", "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "i2", "type": "error", "message": "a CLI warning"}},
            {"type": "item.completed", "item": {"id": "i3", "type": "file_change", "changes": [{"path": "out.txt"}]}},
            {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "first"}},
            {"type": "item.completed", "item": {"id": "i5", "type": "agent_message", "text": "<answer>2</answer>"}},
            {"type": "turn.completed", "usage": {"input_tokens": 24763, "cached_input_tokens": 24448,
                                                 "output_tokens": 122}},
        ]
        out = parse_output("codex", "agent", lines(events))
        self.assertEqual((out.text, out.error, out.finished), ("<answer>2</answer>", None, True))
        self.assertEqual(out.usage, agent_cli.Usage(24763, 122, 24448, 0, None))  # codex reports no cost
        for part in ("**reasoning:** think", "**tool call `shell`:**", "(exit 0)", "**file_change:**", "out.txt"):
            self.assertIn(part, out.trace)
        self.assertNotIn("a CLI warning", out.trace)
        self.assertNotIn("i3", out.trace)

    def test_the_usage_limit_stops_the_run(self):
        out = parse_output("codex", "complete", lines(CODEX_LIMIT_STREAM))
        self.assertEqual((out.error.kind, out.error.message), ("usage_limit", CODEX_LIMIT))
        self.assertTrue(out.error.fatal and out.finished)
        self.assertIsNone(out.usage)

    def test_failures_without_a_known_wording_stay_retryable(self):
        for events in ([{"type": "turn.failed", "error": "plain string"}],
                       [{"type": "turn.failed", "error": {"message": "stream disconnected before completion"}}],
                       [{"type": "thread.started"}, {"type": "error", "message": "Reconnecting... 1/5"}],
                       [{"type": "thread.started"}]):
            error = parse_output("codex", "agent", lines(events)).error
            self.assertTrue(error.retryable and not error.fatal, events)
        self.assertEqual(parse_output("codex", "agent", "").error.kind, "incomplete")
        self.assertEqual(parse_output("codex", "agent", lines([{"type": "thread.started"}])).error.message,
                         "turn did not complete")
        model = [{"type": "turn.failed", "error": {"message": "The 'gpt-x' model is not supported when using Codex"}}]
        self.assertEqual(parse_output("codex", "agent", lines(model)).error.kind, "bad_model")


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        cases = [
            (dict(message=NOT_LOGGED_IN), "auth"),
            (dict(message="x", category="authentication_failed"), "auth"),
            (dict(message="Invalid API key · Fix external API key"), "auth"),
            (dict(message="API Error", status=401), "auth"),
            (dict(message=CLAUDE_LIMIT, category="rate_limit", status=429), "usage_limit"),
            (dict(message=CODEX_LIMIT), "usage_limit"),
            (dict(message="Claude AI usage limit reached|1763300000"), "usage_limit"),
            (dict(message="Credit balance is too low"), "billing"),
            (dict(message="x", category="billing_error"), "billing"),
            (dict(message="x", subtype="error_max_budget_usd"), "cost_cap"),
            (dict(message="x", category="model_not_found"), "bad_model"),
            (dict(message="The model `gpt-9` does not exist or you do not have access to it."), "bad_model"),
            (dict(message="API Error: 429 Too many requests"), "rate_limit"),
            (dict(message="x", status=429), "rate_limit"),
            (dict(message="API Error: 529 Overloaded"), "overloaded"),
            (dict(message="x", status=503), "overloaded"),
            (dict(message="x", category="server_error"), "overloaded"),
            (dict(message="something nobody has seen yet"), "unknown"),
            (dict(message="", category="a_category_from_the_future", status=True), "unknown"),
        ]
        for kwargs, kind in cases:
            self.assertEqual(classify(**kwargs).kind, kind, kwargs)
        self.assertEqual(agent_cli.RETRYABLE & agent_cli.FATAL, frozenset())
        self.assertTrue(classify("something nobody has seen yet").retryable)  # as every failure was before

    def test_exit_without_output(self):
        for stderr in ("error: unknown option '--safe-mode'", "error: unexpected argument '--ephemeral' found",
                       "error: invalid value 'x' for '--sandbox <SANDBOX_MODE>'"):
            error = classify_exit(1, stderr + "\n")
            self.assertEqual((error.kind, error.fatal), ("bad_option", True), stderr)
        self.assertEqual(classify_exit(1, "").kind, "unknown")
        self.assertEqual(classify_exit(1, "Not logged in").kind, "auth")
        self.assertEqual(len(classify_exit(1, "x" * 5000).message), 2000)


class StandaloneTest(unittest.TestCase):
    def test_the_package_imports_only_itself_and_the_standard_library(self):
        """It is copied into other tools as it is (REDESIGN.md, section 7), so it may not need wikiskill."""
        allowed = {"__future__", "collections", "dataclasses", "json", "re", "typing"}
        for path in PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertLessEqual(node.level, 1, f"{path.name} imports from outside the package")
                    names = [] if node.level else [node.module]
                else:
                    continue
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
