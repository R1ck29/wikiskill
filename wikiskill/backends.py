"""LLM backends: the Claude Code CLI (`claude`) and the Codex CLI (`codex`).

WikiSkill never talks to the network itself. Every model call goes through one of
these CLIs, so whatever authentication the CLI already uses (Anthropic, Bedrock,
an internal gateway, ...) is reused as-is.

Two calls are needed:
  complete(prompt, system)        one text answer, no tools (Wiki Maintainer, Skill
                                  Proposer turns, LLM judge)
  run_agent(prompt, system, cwd)  an agentic run with tools inside `cwd`
                                  (Inference Agent); returns the final text and a
                                  readable trace of the whole trajectory

Prompts are sent on stdin and long system prompts through files, so only short
flags appear on the command line (Windows limits command lines to 32k chars and
.cmd shims mangle special characters).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import ConfigError
from .util import read_json

log = logging.getLogger(__name__)

# Defaults verified with claude 2.1.295 and codex-cli 0.146.1. Override them with
# a backend config file (see docs/CONFIGURATION.md) if your CLI version differs.
DEFAULTS: Dict[str, Dict[str, object]] = {
    "claude": {
        "command": "claude",
        "model_flag": "--model",
        "complete_args": ["-p", "--safe-mode", "--output-format", "json", "--tools", "",
                          "--no-session-persistence"],
        "agent_args": ["-p", "--safe-mode", "--output-format", "stream-json", "--verbose",
                       "--permission-mode", "bypassPermissions",
                       "--disallowedTools", "WebFetch,WebSearch", "--no-session-persistence"],
    },
    "codex": {
        "command": "codex",
        "model_flag": "-m",
        "complete_args": ["exec", "--json", "--skip-git-repo-check", "--ephemeral",
                          "--sandbox", "read-only", "-c", "web_search=disabled"],
        "agent_args": ["exec", "--json", "--skip-git-repo-check", "--ephemeral",
                       "--sandbox", "workspace-write", "-c", "web_search=disabled"],
    },
}


_MODEL_RE = re.compile(r"\w[\w.:/@+-]*")  # Anthropic, OpenAI and Bedrock ids / ARNs (never a flag)
_LIVE: set = set()  # running CLI processes, so Ctrl+C in the main thread can stop worker-thread runs
_LIVE_LOCK = threading.Lock()
_STOP = threading.Event()  # set on Ctrl+C: no new CLI starts and no retries until reset_stop()


class BackendError(RuntimeError):
    """The CLI could not be run or returned no usable answer."""


class StoppedError(BackendError):
    """The run was stopped by the user (Ctrl+C); never retried."""


@dataclass(frozen=True)
class AgentResult:
    final_text: str
    trace: str
    ok: bool
    error: str = ""
    infra_error: bool = False  # the CLI itself failed (timeout, API error): retry later, don't score


@dataclass(frozen=True)
class BackendConfig:
    kind: str
    command: str
    model: Optional[str] = None
    model_flag: str = "--model"
    complete_args: Tuple[str, ...] = ()
    agent_args: Tuple[str, ...] = ()
    timeout: int = 1800
    retries: int = 2
    retry_wait: float = 10.0


def load_backend_config(kind: str, model: Optional[str] = None,
                        config_path: Optional[Path] = None) -> BackendConfig:
    """Built-in defaults for `kind`, overridden by the `kind` section of a JSON config file."""
    if kind not in DEFAULTS:
        raise ConfigError(f"unknown backend {kind!r}; choose from {sorted(DEFAULTS)}")
    values = dict(DEFAULTS[kind])
    if config_path:
        overrides = read_json(Path(config_path)).get(kind, {})
        unknown = set(overrides) - set(BackendConfig.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"unknown keys in backend config: {sorted(unknown)}")
        values.update(overrides)
    for key in ("complete_args", "agent_args"):
        values[key] = tuple(str(a) for a in values[key])
    values["kind"] = kind
    if model:
        values["model"] = model
    if values.get("model") and not _MODEL_RE.fullmatch(values["model"]):
        raise ConfigError(f"invalid model name {values['model']!r}")
    return BackendConfig(**values)


class CLIBackend:
    """Shared subprocess handling; subclasses build arguments and parse output."""

    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return f"{self.config.kind}:{self.config.model or 'default-model'}"

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        with tempfile.TemporaryDirectory(prefix="wikiskill-") as tmp:
            args, stdin = self._complete_args(prompt, system, Path(tmp))
            return self._retry(lambda: self._parse_complete(self._run(args, stdin, Path(tmp))))

    def run_agent(self, prompt: str, system: Optional[str], cwd: Path) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="wikiskill-") as tmp:
            args, stdin = self._agent_args(prompt, system, Path(cwd), Path(tmp))
            try:
                return self._retry(lambda: self._parse_agent(self._run(args, stdin, Path(cwd))))
            except BackendError as e:
                return AgentResult("", f"(agent run failed: {e})", ok=False, error=str(e), infra_error=True)

    # ----- subprocess ----------------------------------------------------------
    def _run(self, args: List[str], stdin: str, cwd: Path) -> str:
        exe = shutil.which(self.config.command)
        if not exe:
            raise BackendError(f"command {self.config.command!r} not found on PATH")
        returncode, stdout, stderr = run_process([exe, *args], stdin, cwd, self.config.timeout)
        if returncode is None:
            raise BackendError(f"{self.config.command} timed out after {self.config.timeout}s")
        if returncode != 0 and not stdout.strip():
            raise BackendError(f"{self.config.command} exited {returncode}: {stderr.strip()[-2000:]}")
        return stdout

    def _retry(self, call: Callable):
        attempts = self.config.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return call()
            except BackendError as e:
                if attempt == attempts or _STOP.is_set():
                    raise
                log.warning("%s call failed (attempt %d/%d): %s", self.name, attempt, attempts, e)
                if _STOP.wait(self.config.retry_wait * attempt):  # wakes early on Ctrl+C
                    raise StoppedError("stopped by the user") from e

    def _model_args(self) -> List[str]:
        return [self.config.model_flag, self.config.model] if self.config.model else []

    # ----- implemented by subclasses ---------------------------------------------------
    def _complete_args(self, prompt: str, system: Optional[str], tmp: Path) -> Tuple[List[str], str]:
        raise NotImplementedError

    def _agent_args(self, prompt: str, system: Optional[str], cwd: Path, tmp: Path) -> Tuple[List[str], str]:
        raise NotImplementedError

    def _parse_complete(self, stdout: str) -> str:
        raise NotImplementedError

    def _parse_agent(self, stdout: str) -> AgentResult:
        raise NotImplementedError


def run_process(argv: List[str], stdin: str, cwd: Path, timeout: float) -> Tuple[Optional[int], str, str]:
    """Run a command; on timeout kill its whole process tree. Returns (returncode or None, stdout, stderr).

    npm-installed CLIs are .cmd shims (Windows) or wrapper scripts, so killing only the
    direct child would leave the real agent running and holding the output pipes.
    """
    extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    with _LIVE_LOCK:  # checked under the lock so kill_all_running cannot miss a process
        if _STOP.is_set():
            raise StoppedError("stopped by the user")
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", cwd=str(cwd), **extra)
        _LIVE.add(proc)
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return None, "", ""
    except BaseException:  # Ctrl+C or any error: the CLI runs in its own group, so stop it explicitly
        _kill_tree(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(proc)


def kill_all_running() -> None:
    """Stop every CLI started by run_process and refuse new ones (used when the user presses Ctrl+C)."""
    with _LIVE_LOCK:
        _STOP.set()
        procs = list(_LIVE)
    for proc in procs:
        _kill_tree(proc)


def reset_stop() -> None:
    """Allow CLI runs again (called at the start of each batch of rollouts)."""
    _STOP.clear()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()


def _write_prompt_file(tmp: Path, name: str, text: str) -> str:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _json_lines(stdout: str) -> List[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _block(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}\n{text.rstrip()}\n{fence}"


class ClaudeBackend(CLIBackend):
    """`claude -p`: system prompts via --system-prompt-file / --append-system-prompt-file."""

    def _complete_args(self, prompt, system, tmp):
        args = [*self.config.complete_args, *self._model_args()]
        if system:
            args += ["--system-prompt-file", _write_prompt_file(tmp, "system.md", system)]
        return args, prompt

    def _agent_args(self, prompt, system, cwd, tmp):
        args = [*self.config.agent_args, *self._model_args()]
        if system:  # keep Claude Code's own agent prompt, append the task prompt + skills
            args += ["--append-system-prompt-file", _write_prompt_file(tmp, "system.md", system)]
        return args, prompt

    def _parse_complete(self, stdout):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            raise BackendError(f"claude returned non-JSON output: {stdout[:300]!r}") from None
        if data.get("is_error") or not isinstance(data.get("result"), str):
            raise BackendError(f"claude error: {str(data.get('result'))[:500]}")
        return data["result"]

    def _parse_agent(self, stdout):
        events = _json_lines(stdout)
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result is None:
            raise BackendError("claude stream ended without a result event")
        if result.get("is_error"):  # API / quota / auth failures: retry, never score
            raise BackendError(f"claude error: {str(result.get('result'))[:500]}")
        trace = "\n\n".join(filter(None, (_render_claude_event(e) for e in events)))
        return AgentResult(str(result.get("result", "")), trace, ok=True)


def _render_claude_event(event: dict) -> str:
    kind = event.get("type")
    if kind not in ("assistant", "user"):
        return ""
    content = event.get("message", {}).get("content")
    if not isinstance(content, list):
        return f"**{kind}:** {content}" if content else ""
    parts = []
    for block in content:
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            parts.append(f"**{kind}:** {block['text'].strip()}")
        elif btype == "thinking" and block.get("thinking", "").strip():
            parts.append(f"**reasoning:** {block['thinking'].strip()}")
        elif btype == "tool_use":
            payload = json.dumps(block.get("input", {}), ensure_ascii=False, indent=1)
            parts.append(f"**tool call `{block.get('name')}`:**\n{_block(payload)}")
        elif btype == "tool_result":
            out = block.get("content")
            if isinstance(out, list):
                out = "\n".join(str(c.get("text", c)) for c in out if isinstance(c, dict))
            label = "tool error" if block.get("is_error") else "tool output"
            parts.append(f"**{label}:**\n{_block(str(out))}")
    return "\n\n".join(parts)


class CodexBackend(CLIBackend):
    """`codex exec`: Codex has no system-prompt flag, so the system text leads the stdin prompt."""

    def _complete_args(self, prompt, system, tmp):
        return [*self.config.complete_args, *self._model_args(), "-C", str(tmp), "-"], _join(system, prompt)

    def _agent_args(self, prompt, system, cwd, tmp):
        return [*self.config.agent_args, *self._model_args(), "-C", str(cwd), "-"], _join(system, prompt)

    def _parse_complete(self, stdout):
        return self._parse_agent(stdout).final_text

    def _parse_agent(self, stdout):
        events = _json_lines(stdout)
        if not events:
            raise BackendError(f"codex returned no JSON events: {stdout[:300]!r}")
        failed = next((e for e in events if e.get("type") == "turn.failed"), None)
        items = [e["item"] for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)]
        messages = [i.get("text", "") for i in items if i.get("type") == "agent_message"]
        trace = "\n\n".join(filter(None, (_render_codex_item(i) for i in items)))
        if failed or not any(e.get("type") == "turn.completed" for e in events):
            error = (failed or {}).get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise BackendError(f"codex error: {str(message or 'turn did not complete')[:500]}")
        return AgentResult(messages[-1] if messages else "", trace, ok=True)


def _join(system: Optional[str], prompt: str) -> str:
    return f"{system.strip()}\n\n---\n\n{prompt}" if system else prompt


def _render_codex_item(item: dict) -> str:
    itype = item.get("type")
    if itype == "agent_message":
        return f"**assistant:** {item.get('text', '').strip()}"
    if itype == "reasoning":
        return f"**reasoning:** {item.get('text', '').strip()}"
    if itype == "command_execution":
        return (f"**tool call `shell`:**\n{_block(str(item.get('command')))}\n\n"
                f"**tool output (exit {item.get('exit_code')}):**\n{_block(str(item.get('aggregated_output', '')))}")
    if itype == "error":
        return ""  # CLI warnings (hooks, model metadata) are not part of the agent's trajectory
    payload = {k: v for k, v in item.items() if k != "id"}
    return f"**{itype}:**\n{_block(json.dumps(payload, ensure_ascii=False, indent=1))}"


BACKENDS = {"claude": ClaudeBackend, "codex": CodexBackend}


def make_backend(kind: str, model: Optional[str] = None, config_path: Optional[Path] = None) -> CLIBackend:
    return BACKENDS[kind](load_backend_config(kind, model, config_path))

