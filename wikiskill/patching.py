"""Patch-based text editing shared by the Wiki Maintainer and the Skill Proposer.

Operations follow the paper's prompts (Appendix E.2/E.3):
  {"op": "append", "content": ...}
  {"op": "replace", "target": <exact substring>, "content": ...}
  {"op": "insert_after", "target": <exact substring>, "content": ...}
"""
from __future__ import annotations

import difflib
from typing import Iterable


class PatchError(ValueError):
    """An edit could not be applied (unknown op, missing or ambiguous target)."""


def apply_edit(text: str, edit: dict) -> str:
    if not isinstance(edit, dict):
        raise PatchError(f"edit must be an object, got {type(edit).__name__}")
    op, content = edit.get("op"), edit.get("content")
    if not isinstance(content, str):
        raise PatchError(f"edit {op!r} needs a string 'content'")
    if op == "append":
        if text and not text.endswith("\n"):
            text += "\n"
        return text + content + ("" if content.endswith("\n") else "\n")
    if op not in ("replace", "insert_after"):
        raise PatchError(f"unknown op {op!r} (use append, replace, insert_after)")
    target = edit.get("target")
    if not isinstance(target, str) or not target:
        raise PatchError(f"edit {op!r} needs a non-empty string 'target'")
    count = _count_overlapping(text, target)
    if count == 0:
        raise PatchError(f"target not found: {target[:80]!r}")
    if count > 1:
        raise PatchError(f"target is ambiguous (several matches); use a longer exact target: {target[:80]!r}")
    if op == "replace":
        return text.replace(target, content, 1)
    return text.replace(target, target + content, 1)


def _count_overlapping(text: str, target: str) -> int:
    count, start = 0, text.find(target)
    while start != -1 and count < 2:
        count, start = count + 1, text.find(target, start + 1)
    return count


def apply_edits(text: str, edits: Iterable[dict]) -> str:
    """Apply all edits in order; any failure raises PatchError and nothing is returned."""
    if not isinstance(edits, list) or not edits:
        raise PatchError("'edits' must be a non-empty list")
    for edit in edits:
        text = apply_edit(text, edit)
    return text


def unified_diff(old: str, new: str, path: str, is_new: bool = False) -> str:
    """Unified diff of one file; a created file is diffed against /dev/null (as in Figure 3)."""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="/dev/null" if is_new else f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
