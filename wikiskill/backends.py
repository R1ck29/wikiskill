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

A `Budget` caps what one day of budgeted (automatic) runs may spend, measured from the
cost each CLI call reports.

What the command lines look like and what their output means lives in `agent_cli`; this module
runs the processes and owns the policy: config files, retries, budgets, which failures stop a run.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import agent_cli
from .agent_cli import CallSpec, CliInfo
from .config import ConfigError
from .util import locked, read_json, write_json

log = logging.getLogger(__name__)

# The arguments of a CLI whose version is not known: the ones verified with claude 2.1.295 and
# codex-cli 0.146.1. Once the version is known (CLIBackend.detect) agent_cli adapts them to it.
# A backend config file replaces them and switches the adapting off (see docs/CONFIGURATION.md).
DEFAULTS: dict[str, dict[str, object]] = {
    kind: {"command": kind, "model_flag": model_flag,
           **{f"{mode}_args": list(agent_cli.base_args(kind, mode)) for mode in agent_cli.MODES}}
    for kind, model_flag in (("claude", "--model"), ("codex", "-m"))
}
VERSION_TIMEOUT = 15  # seconds `<command> --version` may take


_MODEL_RE = re.compile(r"\w[\w.:/@+-]*")  # Anthropic, OpenAI and Bedrock ids / ARNs (never a flag)
_LIVE: set = set()  # running CLI processes, so Ctrl+C in the main thread can stop worker-thread runs
_LIVE_LOCK = threading.Lock()
_STOP = threading.Event()  # set on Ctrl+C: no new CLI starts and no retries until reset_stop()
# what to do about a failure that stops the run ({cli} is the command)
_ADVICE = {
    "auth": "make `{cli}` work on its own (log in or set the API key), then run the same command again",
    "billing": "the account behind `{cli}` cannot be charged; fix that, then run the same command again",
    "bad_option": "this `{cli}` version does not take these arguments; set the ones it takes in a backend "
                  "config (docs/CONFIGURATION.md, section 3)",
    "bad_model": "choose a model this `{cli}` can use with --model",
}


class BackendError(RuntimeError):
    """The CLI could not be run or returned no usable answer.

    `kind` is one of agent_cli.errors: it says whether trying again can help (`retryable`) and whether
    the whole run must stop because every further call would fail the same way (`fatal`).
    """

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in agent_cli.RETRYABLE

    @property
    def fatal(self) -> bool:
        return self.kind in agent_cli.FATAL


class StoppedError(BackendError):
    """The run was stopped by the user (Ctrl+C); never retried."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="stopped")


class BudgetExceeded(RuntimeError):
    """Today's budget is spent: the run stops (not retried, nothing scored) and resumes from its saved stage."""


class UsageLimitReached(BudgetExceeded):
    """The CLI's own usage limit is reached: the run stops like a spent budget and resumes after the reset."""


class Budget:
    """A daily limit in USD for budgeted runs, kept in a ledger file (<workspace>/spend.json).

    Spend is the cost each CLI call reports. A CLI that reports none (codex) cannot be
    measured, so it gets one budgeted run a day instead. A `shared` budget (the total of all
    automatic runs) is charged and checked together with this one. Ledger updates hold an OS
    lock, so runs of other workspaces that share a ledger never overwrite each other's spend.
    """

    def __init__(self, path: Path, limit: float, shared: Budget | None = None, label: str = "today's budget") -> None:
        self.path, self.limit, self.shared, self.label = Path(path), limit, shared, label
        self._lock = threading.Lock()  # rollout workers share one budget

    def today(self) -> dict[str, Any]:
        """Today's ledger: spend in USD, calls that reported no cost, and runs started."""
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            data = None
        day = {"date": time.strftime("%Y-%m-%d"), "usd": 0.0, "unpriced_calls": 0, "runs": 0}
        if isinstance(data, dict) and data.get("date") == day["date"]:
            for key, default in day.items():
                if isinstance(data.get(key), type(default)):
                    day[key] = data[key]
        return day

    def blocked(self) -> str | None:
        """Why no new budgeted run may start today, or None."""
        day = self.today()
        if day["usd"] >= self.limit:
            return self._spent(day)
        if day["unpriced_calls"] and day["runs"]:
            return "the CLI reports no cost, so budgeted runs are limited to one a day"
        return self.shared.blocked() if self.shared else None

    def start_run(self) -> None:
        reason = self.blocked()
        if reason:
            raise BudgetExceeded(reason)
        for budget in self._chain():
            with budget._update() as day:
                day["runs"] += 1

    def check(self) -> None:
        """Raise BudgetExceeded before a call once today's spend reached a limit."""
        for budget in self._chain():
            day = budget.today()
            if day["usd"] >= budget.limit:
                raise BudgetExceeded(budget._spent(day))

    def remaining(self) -> float:
        """USD left today before the nearest limit (this budget's or the shared one's); never below 0."""
        return max(0.0, min(budget.limit - budget.today()["usd"] for budget in self._chain()))

    def _spent(self, day: dict[str, Any]) -> str:
        return f"{self.label} of ${self.limit:.2f} is spent (${day['usd']:.2f})"

    def add(self, usd: float | None) -> None:
        for budget in self._chain():
            with budget._update() as day:
                if usd is None:
                    day["unpriced_calls"] += 1
                else:
                    day["usd"] = round(day["usd"] + usd, 6)

    def _chain(self) -> list[Budget]:
        return [self] + ([self.shared] if self.shared else [])

    @contextlib.contextmanager
    def _update(self) -> Iterator[dict[str, Any]]:
        """Today's ledger to change in place; written back under the thread and OS locks."""
        with self._lock, contextlib.ExitStack() as stack:
            with contextlib.suppress(OSError):  # a drive without file locks: count without the OS lock
                self.path.parent.mkdir(parents=True, exist_ok=True)
                stack.enter_context(locked(self.path.with_name(self.path.name + ".lock")))
            day = self.today()
            yield day
            write_json(self.path, day)


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
    model: str | None = None
    model_flag: str = "--model"
    complete_args: tuple[str, ...] = ()
    agent_args: tuple[str, ...] = ()
    timeout: int = 1800
    retries: int = 2
    retry_wait: float = 10.0
    # the modes ("complete", "agent") whose arguments a config file wrote: used as written, never adapted
    pinned: frozenset[str] = frozenset()


def load_backend_config(kind: str, model: str | None = None,
                        config_path: Path | None = None) -> BackendConfig:
    """Built-in defaults for `kind`, overridden by the `kind` section of a JSON config file."""
    if kind not in DEFAULTS:
        raise ConfigError(f"unknown backend {kind!r}; choose from {sorted(DEFAULTS)}")
    values = dict(DEFAULTS[kind])
    if config_path:
        overrides = read_json(Path(config_path)).get(kind, {})
        unknown = set(overrides) - (set(BackendConfig.__dataclass_fields__) - {"pinned"})
        if unknown:
            raise ConfigError(f"unknown keys in backend config: {sorted(unknown)}")
        values.update(overrides)
        values["pinned"] = frozenset(mode for mode in agent_cli.MODES if f"{mode}_args" in overrides)
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

    reports_cost = False  # does the CLI report the cost of each call (see Budget)?

    def __init__(self, config: BackendConfig, budget: Budget | None = None) -> None:
        self.config = config
        self.budget = budget
        self.cli: CliInfo = agent_cli.unknown(config.kind)  # the version, once detect() has looked
        self._detected = False

    @property
    def name(self) -> str:
        return f"{self.config.kind}:{self.config.model or 'default-model'}"

    def complete(self, prompt: str, system: str | None = None) -> str:
        with tempfile.TemporaryDirectory(prefix="wikiskill-") as tmp:
            return self._retry(lambda: self._call("complete", prompt, system, Path(tmp), Path(tmp))).text

    def run_agent(self, prompt: str, system: str | None, cwd: Path) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="wikiskill-") as tmp:
            try:
                outcome = self._retry(lambda: self._call("agent", prompt, system, Path(cwd), Path(tmp)))
            except BackendError as e:
                if e.fatal:  # every other task would fail the same way: stop the run, do not score an outage
                    raise
                return AgentResult("", f"(agent run failed: {e})", ok=False, error=str(e), infra_error=True)
            return AgentResult(outcome.text, outcome.trace, ok=True)

    # ----- version and arguments ---------------------------------------------------
    def detect(self) -> CliInfo:
        """Ask the CLI for its version (`--version`, once) so the arguments fit it; unknown when that fails."""
        exe = shutil.which(self.config.command)
        if self._detected or not exe:
            return self.cli
        try:
            _, stdout, stderr = run_process([exe, *agent_cli.VERSION_ARGS], "", Path(tempfile.gettempdir()),
                                            VERSION_TIMEOUT)
        except StoppedError:
            raise
        except (BackendError, OSError) as e:
            stdout, stderr = "", str(e)
        self.cli = agent_cli.parse_version(self.config.kind, stdout or stderr)
        self._detected = True
        log.debug("%s is %s", self.config.command, self.cli.label())
        return self.cli

    def base_args(self, mode: str) -> tuple[str, ...]:
        """The fixed arguments of a call: the config file's as written, else the ones for this CLI version."""
        if mode in self.config.pinned:
            return getattr(self.config, f"{mode}_args")
        return agent_cli.base_args(self.config.kind, mode, self.cli)

    def describe(self) -> list[str]:
        """Lines for `check`: the version found and how the arguments of each call are chosen."""
        lines = [f"version: {self.cli.label()}"]
        if self.cli.version is None and not self.config.pinned:
            lines.append(f"  the version could not be read: using the arguments verified with "
                         f"{self.config.kind} {agent_cli.VERIFIED[self.config.kind]}")
        for mode in agent_cli.MODES:
            source = ("set by the backend config, used as written" if mode in self.config.pinned
                      else "chosen for this version" if self.cli.version else "defaults")
            shown = " ".join(arg or '""' for arg in self.base_args(mode))
            lines.append(f"{mode} arguments ({source}): {shown}")
        return lines

    def _args(self, mode: str, prompt: str, system: str | None, cwd: Path, tmp: Path) -> tuple[list[str], str]:
        """The command line and the stdin text of one call."""
        kind = self.config.kind
        system_file = _write_prompt_file(tmp, "system.md", system) if system and kind == "claude" else None
        # one call may not spend more than what is left of today's budget (config-file arguments stay as written)
        cap = self.budget.remaining() if self.budget and mode not in self.config.pinned else None
        spec = CallSpec(mode, self.config.model, system_file, str(cwd), cap)
        args = agent_cli.call_args(kind, self.base_args(mode), spec, self.cli, self.config.model_flag)
        return args, agent_cli.join_prompt(kind, system, prompt)

    def _call(self, mode: str, prompt: str, system: str | None, cwd: Path, tmp: Path) -> agent_cli.Outcome:
        if self.budget:
            self.budget.check()
        args, stdin = self._args(mode, prompt, system, cwd, tmp)
        outcome = agent_cli.parse_output(self.config.kind, mode, self._run(args, stdin, cwd))
        self._charge(outcome)
        if outcome.error:
            self._fail(outcome.error)
        return outcome

    def _charge(self, outcome: agent_cli.Outcome) -> None:
        """Add the call to today's ledger. Calls that fail with output cost too; a cut-off call adds nothing,
        and neither does a call the CLI refused before the model did any work (a usage limit, a bad login)."""
        if not self.budget:
            return
        cost = outcome.usage.cost_usd if outcome.usage else None
        if cost is not None:
            self.budget.add(cost)
        elif not self.reports_cost and not (outcome.error and outcome.error.fatal):
            self.budget.add(None)

    def _fail(self, error: agent_cli.CallError) -> None:
        """Raise the exception for a failure the CLI reported in its output."""
        cli, message = self.config.command, f"{self.config.kind} error: {error.message}"
        if error.kind == "usage_limit":
            raise UsageLimitReached(f"the usage limit of `{cli}` is reached ({error.message})")
        if error.kind == "cost_cap":
            raise BudgetExceeded(f"one call used up what was left of {self.budget.label if self.budget else 'the budget'}")
        raise BackendError(_with_advice(message, error.kind, cli), error.kind)

    # ----- subprocess ----------------------------------------------------------
    def _run(self, args: list[str], stdin: str, cwd: Path) -> str:
        exe = shutil.which(self.config.command)
        if not exe:
            raise BackendError(f"command {self.config.command!r} not found on PATH", "not_found")
        returncode, stdout, stderr = run_process([exe, *args], stdin, cwd, self.config.timeout)
        if returncode is None:
            raise BackendError(f"{self.config.command} timed out after {self.config.timeout}s", "timeout")
        if returncode != 0 and not stdout.strip():
            error = agent_cli.classify_exit(returncode, stderr)
            message = f"{self.config.command} exited {returncode}: {error.message}"
            if error.kind == "usage_limit":
                raise UsageLimitReached(f"the usage limit of `{self.config.command}` is reached ({error.message})")
            raise BackendError(message, error.kind)
        return stdout

    def _retry(self, call: Callable):
        attempts = self.config.retries + 1
        attempt = 1
        while True:
            try:
                return call()
            except BackendError as e:
                if e.kind == "bad_option" and self._adapt():
                    log.warning("%s rejected an argument (%s); trying again with the arguments for %s",
                                self.config.command, e, self.cli.label())
                    continue  # not counted as an attempt: the arguments changed, and this happens once
                if not e.retryable:  # waiting does not help: a login, a model name, a missing command
                    raise BackendError(_with_advice(str(e), e.kind, self.config.command), e.kind) from e
                if attempt == attempts or _STOP.is_set():
                    raise
                log.warning("%s call failed (attempt %d/%d): %s", self.name, attempt, attempts, e)
                if _STOP.wait(self.config.retry_wait * attempt):  # wakes early on Ctrl+C
                    raise StoppedError("stopped by the user") from e
                attempt += 1

    def _adapt(self) -> bool:
        """After the CLI rejected an argument: look up its version if nobody did; True when that changes
        the arguments of a call (a library user who never called detect() gets the right ones this way)."""
        if self._detected:
            return False
        before = [self.base_args(mode) for mode in agent_cli.MODES]
        self.detect()
        return before != [self.base_args(mode) for mode in agent_cli.MODES]

    # ----- kept for callers of 0.11.0 --------------------------------------------------
    def _parse_complete(self, stdout: str) -> str:
        return self._parsed("complete", stdout).text

    def _parse_agent(self, stdout: str) -> AgentResult:
        outcome = self._parsed("agent", stdout)
        return AgentResult(outcome.text, outcome.trace, ok=True)

    def _parsed(self, mode: str, stdout: str) -> agent_cli.Outcome:
        outcome = agent_cli.parse_output(self.config.kind, mode, stdout)
        if outcome.error:
            self._fail(outcome.error)
        return outcome

    def _cost(self, stdout: str) -> float | None:
        """USD the call reports, or None when the CLI reports no cost."""
        usage = agent_cli.parse_output(self.config.kind, "agent", stdout).usage
        return usage.cost_usd if usage else None


def _with_advice(message: str, kind: str, command: str) -> str:
    advice = _ADVICE.get(kind, "").format(cli=command)
    return f"{message}; {advice}" if advice and advice not in message else message


def run_process(argv: list[str], stdin: str, cwd: Path, timeout: float) -> tuple[int | None, str, str]:
    """Run a command; on timeout kill its whole process tree. Returns (returncode or None, stdout, stderr).

    npm-installed CLIs are .cmd shims (Windows) or wrapper scripts, so killing only the
    direct child would leave the real agent running and holding the output pipes.
    """
    extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt"
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
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=10)
        return None, "", ""
    except BaseException:  # Ctrl+C or any error: the CLI runs in its own group, so stop it explicitly
        _kill_tree(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
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


class ClaudeBackend(CLIBackend):
    """`claude -p`: system prompts via --system-prompt-file / --append-system-prompt-file."""

    reports_cost = True


class CodexBackend(CLIBackend):
    """`codex exec`: Codex has no system-prompt flag, so the system text leads the stdin prompt."""


BACKENDS = {"claude": ClaudeBackend, "codex": CodexBackend}


def make_backend(kind: str, model: str | None = None, config_path: Path | None = None,
                 budget: Budget | None = None) -> CLIBackend:
    return BACKENDS[kind](load_backend_config(kind, model, config_path), budget)

