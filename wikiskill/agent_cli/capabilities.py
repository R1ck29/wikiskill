"""Which CLI version is installed, what it can do, and the arguments to use with it.

The argument lists of an unknown version are the ones wikiskill 0.11.0 used (verified with
claude 2.1.295 and codex-cli 0.146.1, and still accepted by claude 2.1.325 and codex-cli 0.169.0).
A capability changes them only once the version is known to have it, so a version that cannot
be read never makes things worse than before.

To follow a new CLI release, add a row to CAPABILITIES and use it in base_args / call_args.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

KINDS = ("claude", "codex")
VERSION_ARGS = ("--version",)  # prints e.g. "2.1.325 (Claude Code)" / "codex-cli 0.169.0"
VERIFIED = {"claude": "2.1.295 to 2.1.325", "codex": "0.146.1 to 0.169.0"}  # versions the arguments were run on
_VERSION_RE = {
    "claude": re.compile(r"^\s*v?(\d+(?:\.\d+){1,3})\s*\(Claude Code\)", re.MULTILINE),
    "codex": re.compile(r"^\s*codex(?:-cli)?\s+v?(\d+(?:\.\d+){1,3})", re.MULTILINE),
}
Version = tuple[int, ...]

# capability -> the first version that has it. (0,) means: every version, once the version is known.
CAPABILITIES: dict[str, dict[str, Version]] = {
    "claude": {
        "max_budget_usd": (2, 0, 28),  # --max-budget-usd: a spending cap for one -p call
        "safe_mode": (2, 1, 169),  # --safe-mode: no personal CLAUDE.md, skills, plugins, hooks or MCP servers
        # tools that act outside the task folder or after the run (measured on 2.1.325: all offered under
        # --safe-mode, and names a version does not have are accepted in --disallowedTools)
        "outside_tools": (0,),
    },
    "codex": {
        # 0.169.0 has --ignore-user-config and --trust. Neither is used: the user's config.toml may hold the
        # model provider (a gateway), and --trust would save trust for every temporary task folder.
    },
}
WEB_TOOLS = ("WebFetch", "WebSearch")
OUTSIDE_TOOLS = ("CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "PushNotification", "SendMessage",
                 "RemoteTrigger")
MODES = ("complete", "agent")


@dataclass(frozen=True)
class CliInfo:
    """The CLI behind a command: `version` is None until `--version` was run and understood."""

    kind: str
    version: Version | None = None
    raw: str = ""  # the first line `--version` printed

    def has(self, capability: str) -> bool:
        since = CAPABILITIES[self.kind].get(capability)
        return self.version is not None and since is not None and self.version >= since

    def label(self) -> str:
        if self.version is None:
            return f"{self.kind} (version unknown{f': {self.raw!r}' if self.raw else ''})"
        return f"{self.kind} {'.'.join(map(str, self.version))}"


def unknown(kind: str) -> CliInfo:
    _check_kind(kind)
    return CliInfo(kind)


def parse_version(kind: str, output: str) -> CliInfo:
    """Read the output of `<command> --version`; anything else (another program, an error) is version None."""
    _check_kind(kind)
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")[:80]
    match = _VERSION_RE[kind].search(output)
    return CliInfo(kind, tuple(int(part) for part in match.group(1).split(".")) if match else None, first)


@dataclass(frozen=True)
class CallSpec:
    """One call, described by what is wanted instead of by flags."""

    mode: str  # "complete": one text answer without tools | "agent": work with tools in a folder
    model: str | None = None
    system_file: str | None = None  # claude: file holding the system prompt
    cwd: str | None = None  # codex: the working root (-C)
    max_cost_usd: float | None = None  # stop this one call at this cost, where the CLI can


def base_args(kind: str, mode: str, info: CliInfo | None = None) -> tuple[str, ...]:
    """The fixed arguments of a `mode` call for this CLI version (what a backend config may replace)."""
    _check_kind(kind)
    if mode not in MODES:
        raise ValueError(f"unknown call mode {mode!r}")
    info = info or unknown(kind)
    if kind == "codex":
        sandbox = "read-only" if mode == "complete" else "workspace-write"
        return ("exec", "--json", "--skip-git-repo-check", "--ephemeral", "--sandbox", sandbox,
                "-c", "web_search=disabled")
    # an unknown version gets --safe-mode as before; only a version known to be too old loses it
    safe = () if info.version is not None and not info.has("safe_mode") else ("--safe-mode",)
    if mode == "complete":
        return ("-p", *safe, "--output-format", "json", "--tools", "", "--no-session-persistence")
    denied = WEB_TOOLS + (OUTSIDE_TOOLS if info.has("outside_tools") else ())
    return ("-p", *safe, "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions",
            "--disallowedTools", ",".join(denied), "--no-session-persistence")


def call_args(kind: str, base: Sequence[str], spec: CallSpec, info: CliInfo | None = None,
              model_flag: str | None = None) -> list[str]:
    """`base` plus what changes from call to call: model, system prompt file, folder, cost cap."""
    _check_kind(kind)
    info = info or unknown(kind)
    args = list(base)
    if spec.model:
        args += [model_flag or ("--model" if kind == "claude" else "-m"), spec.model]
    if kind == "codex":  # no system-prompt flag: join_prompt puts the system text in front of the prompt
        return [*args, "-C", str(spec.cwd), "-"]
    if spec.system_file:  # an agent keeps Claude Code's own prompt and gets the task prompt + skills appended
        args += ["--system-prompt-file" if spec.mode == "complete" else "--append-system-prompt-file",
                 spec.system_file]
    if spec.max_cost_usd is not None and info.has("max_budget_usd"):
        args += ["--max-budget-usd", f"{max(spec.max_cost_usd, 0.01):.2f}"]
    return args


def join_prompt(kind: str, system: str | None, prompt: str) -> str:
    """The text to send on stdin: codex has no system-prompt flag, so its system text leads the prompt."""
    if kind == "codex" and system:
        return f"{system.strip()}\n\n---\n\n{prompt}"
    return prompt


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown CLI {kind!r}; choose from {list(KINDS)}")
