"""What WikiSkill knows about the `claude` and `codex` command lines.

    capabilities  which version is installed, what it can do, the arguments to use with it
    parsing       final text, readable trace and usage from what a call printed
    errors        the kind of a failure: does waiting help, must the whole run stop

Everything here is a function from text to data: nothing starts a process, reads a file or keeps
state, and nothing imports from wikiskill. The caller (wikiskill/backends.py) runs the commands and
owns retries, budgets and config files. This package is the in-repository forerunner of the shared
`agent_cli` library (docs/design/REDESIGN.md, section 7) and is meant to be replaced by a copy of it.
"""
from .capabilities import (
    CAPABILITIES,
    KINDS,
    MODES,
    VERIFIED,
    VERSION_ARGS,
    CallSpec,
    CliInfo,
    base_args,
    call_args,
    join_prompt,
    parse_version,
    unknown,
)
from .errors import FATAL, RETRYABLE, CallError, classify, classify_exit
from .parsing import Outcome, Usage, json_lines, parse_output

__all__ = [
    "CAPABILITIES", "FATAL", "KINDS", "MODES", "RETRYABLE", "VERIFIED", "VERSION_ARGS",
    "CallError", "CallSpec", "CliInfo", "Outcome", "Usage",
    "base_args", "call_args", "classify", "classify_exit", "join_prompt", "json_lines", "parse_output",
    "parse_version", "unknown",
]
