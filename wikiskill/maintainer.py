"""Wiki Maintainer: consolidate sampled traces into the persistent wiki (paper §3.2.2).

One LLM call per iteration returns JSON edits (create/update pattern pages, rewrite
index.md, append to logs.md) that are applied incrementally. The wiki is never reset.
"""
from __future__ import annotations

import logging
import random
import re
from pathlib import Path
from typing import Any

from .patching import PatchError, apply_edits
from .scoring import Outcome
from .util import append_text, extract_json_object, is_windows_reserved, read_text, truncate, write_text
from .workspace import Workspace

log = logging.getLogger(__name__)

PROMPT = (Path(__file__).parent / "prompts" / "wiki_maintainer.md").read_text(encoding="utf-8")
PATTERN_NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\.md")
MAX_FAIL, MAX_PASS, TRACE_CHARS = 5, 3, 15000  # Appendix C


class MaintainerError(RuntimeError):
    """The model never returned usable wiki edits."""


def sample_traces(outcomes: dict[str, Outcome], seed: int) -> list[str]:
    """Stratified sample: up to 5 failing and up to 3 passing traces (Appendix C)."""
    rng = random.Random(seed)
    failing = sorted(t for t, o in outcomes.items() if not o.passed)
    passing = sorted(t for t, o in outcomes.items() if o.passed)
    return (rng.sample(failing, min(MAX_FAIL, len(failing)))
            + rng.sample(passing, min(MAX_PASS, len(passing))))


def wiki_context(ws: Workspace) -> str:
    parts = [f"### wiki/{name}\n{read_text(ws.wiki / name)}" for name in ("index.md", "logs.md", "skill-impact.md")]
    for page in sorted((ws.wiki / "patterns").glob("*.md")):
        parts.append(f"### wiki/patterns/{page.name}\n{read_text(page)}")
    return "\n\n".join(parts)


def build_prompt(ws: Workspace, iteration: int, traces: dict[str, str],
                 skills: list[tuple[str, str]]) -> str:
    skill_text = "\n\n".join(f"### skills/{n}/SKILL.md\n{t}" for n, t in skills) or "(no active skills yet)"
    trace_text = "\n\n".join(
        f"### Trace {task_id}\n{truncate(text, TRACE_CHARS)}" for task_id, text in traces.items()
    ) or "(no traces)"
    return (
        f"# Iteration {iteration}\n\n## Current wiki\n\n{wiki_context(ws)}\n\n"
        f"## Active skills (what the agent was given)\n\n{skill_text}\n\n"
        f"## Execution traces from this iteration\n\n{trace_text}\n"
    )


def run_maintainer(backend: Any, ws: Workspace, iteration: int, traces: dict[str, str],
                   skills: list[tuple[str, str]], attempts: int = 3) -> list[str]:
    """Ask the model for wiki edits and apply them. Returns warnings about skipped edits."""
    prompt = build_prompt(ws, iteration, traces, skills)
    error = ""
    for _ in range(attempts):
        reply = backend.complete(prompt + error, system=PROMPT)
        try:
            data = extract_json_object(reply)
            _check_required(data)
        except ValueError as e:
            error = f"\n\nYour previous reply was unusable ({e}). Reply with the JSON object only."
            log.warning("wiki maintainer reply unusable: %s", e)
            continue
        return apply_output(ws, iteration, data)
    raise MaintainerError(f"wiki maintainer gave no usable JSON after {attempts} attempts; run again to retry")


def _check_required(data: dict[str, Any]) -> None:
    for key in ("update_index", "append_log"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"'{key}' is required and must be a non-empty string")
    for key in ("create_patterns", "update_patterns"):
        if not isinstance(data.get(key, []), list):
            raise ValueError(f"'{key}' must be a list")  # noqa: TRY004 -- callers retry on ValueError


def apply_output(ws: Workspace, iteration: int, data: dict[str, Any]) -> list[str]:
    """Apply create/update/index/log edits; a bad entry is skipped and reported, never fatal."""
    patterns, warnings = ws.wiki / "patterns", []
    for entry in data.get("create_patterns", []):
        name = _pattern_name(entry, warnings)
        if name is None:
            continue
        if not isinstance(entry.get("content"), str):
            warnings.append(f"create_patterns: {name} has no text content; skipped")
            continue
        if (patterns / name).exists():  # the prompt forbids duplicates; keep the old page and add to it
            warnings.append(f"create_patterns: {name} already exists; appended instead of overwriting")
            append_text(patterns / name, f"\n## Update (iteration {iteration})\n\n{entry['content']}\n")
        else:
            write_text(patterns / name, entry["content"].rstrip("\n") + "\n")
    for entry in data.get("update_patterns", []):
        name = _pattern_name(entry, warnings)
        if name is None:
            continue
        if not (patterns / name).is_file():
            warnings.append(f"update_patterns: {name} does not exist; skipped")
            continue
        try:
            write_text(patterns / name, apply_edits(read_text(patterns / name), entry.get("edits")))
        except PatchError as e:
            warnings.append(f"update_patterns: {name} skipped ({e})")
    write_text(ws.wiki / "index.md", data["update_index"].rstrip("\n") + "\n")
    notes = "".join(f"\n- (harness) {w}" for w in warnings)
    append_text(ws.wiki / "logs.md", f"\n## Iteration {iteration}\n\n{data['append_log'].strip()}\n{notes}\n")
    return warnings


def _pattern_name(entry: Any, warnings: list[str]) -> Any:
    name = entry.get("name") if isinstance(entry, dict) else None
    if isinstance(name, str) and name.startswith("wiki/patterns/"):
        name = name[len("wiki/patterns/"):]
    if not isinstance(name, str) or not PATTERN_NAME_RE.fullmatch(name) or is_windows_reserved(name):
        warnings.append(f"invalid pattern name {name!r}; skipped")
        return None
    return name
