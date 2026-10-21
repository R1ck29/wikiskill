"""Learning from real usage: queue feedback on real results, then keep evolving the same workspace.

`feedback` appends one judged real-world result to `<suite>/feedback.jsonl` (the pending queue).
`improve` moves pending rows into the train/val tasks once the current round is finished
(`merge_ready`), and the changed task set starts a new round in the same workspace
(EvolveOptions.adopt_new_tasks), so skills, wiki and history carry on.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import ANSWER_REQUIRED, ConfigError, Suite
from .evolve import is_done
from .skills import copy_skills, exported_by_wikiskill
from .util import append_text, backup_once, is_windows_reserved, read_text, write_text
from .workspace import Workspace

FEEDBACK = "feedback.jsonl"
MERGED = "feedback-merged.jsonl"  # history of rows already moved into the tasks file
FEEDBACK_FILES = "fb"  # input files of feedback rows: <files_dir>/fb/<id>/<name>
MIN_NEW = 3  # pending feedback rows that start a new round (improve --min-new default)


def feedback_row(scorer_type: str, prompt: str, verdict: str, output: str | None = None,
                 correct: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Build the task fields (without id/files/split) for one judged real result."""
    if not prompt.strip():
        raise ConfigError("feedback needs the request text (--prompt)")
    if verdict == "good":
        if not output:
            raise ConfigError("a 'good' verdict needs the accepted result (--output)")
        row: dict[str, Any] = {"prompt": prompt, "answer": output}
    elif verdict == "bad" and correct:
        row = {"prompt": prompt, "answer": correct}
    elif verdict == "bad":
        if scorer_type in ANSWER_REQUIRED:
            raise ConfigError(f"scorer {scorer_type!r} needs the correct answer for a 'bad' verdict (--correct)")
        rejected = f" The rejected response was: {output}" if output else ""
        row = {"prompt": prompt, "rubric": f"A user rejected a response to this request as wrong.{rejected}"}
    else:
        raise ConfigError("--verdict must be 'good' or 'bad'")
    if scorer_type == "regex" and "answer" in row:  # a real result is literal text, not a pattern
        row["answer"] = re.escape(row["answer"])
    if note:
        row["rubric"] = (row.get("rubric", "") + f" User's note: {note}").strip()
    return row


def record_feedback(suite: Suite, prompt: str, verdict: str, output: str | None = None,
                    correct: str | None = None, note: str | None = None,
                    files: Sequence[Path] = ()) -> tuple[str, int]:
    """Append one row to the pending queue; return (task id, number of pending rows)."""
    row = feedback_row(suite.scorer["type"], prompt, verdict, output, correct, note)
    paths = [Path(f) for f in files]
    for path in paths:
        if not path.is_file():
            raise ConfigError(f"--file {path} is not a file")
        if is_windows_reserved(path.name):
            raise ConfigError(f"--file {path.name} cannot be used on Windows; rename it")
    if len({p.name.lower() for p in paths}) != len(paths):
        raise ConfigError("--file names must be different")
    task_id = f"fb-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    rel = []
    for path in paths:
        target = suite.files_dir / FEEDBACK_FILES / task_id / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        rel.append(f"{FEEDBACK_FILES}/{task_id}/{path.name}")
    row = {"id": task_id, **row}
    if rel:
        row["files"] = rel
    append_text(suite.root / FEEDBACK, json.dumps(row, ensure_ascii=False) + "\n")
    return task_id, len(pending(suite.root))


def pending(suite_root: Path) -> list[dict[str, Any]]:
    """Feedback rows not yet merged into the tasks file."""
    path = Path(suite_root) / FEEDBACK
    return _rows(read_text(path)) if path.is_file() else []


def _rows(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ----- improve: rounds ----------------------------------------------------------------
def resolve_workspace(root: Path) -> Workspace:
    """The workspace at `root`, or its newest gen-NNN folder (layout written by improve in 0.2.x)."""
    gens = sorted(p for p in Path(root).glob("gen-[0-9][0-9][0-9]") if p.is_dir())
    return Workspace(gens[-1] if gens else root)


def merge_ready(suite_root: Path, tasks_path: Path, state: dict[str, Any] | None, iterations: int,
                min_new: int) -> int:
    """Merge pending feedback before the first run, or once the round is finished and enough rows wait."""
    if state is not None and (not is_done(state, iterations) or len(pending(suite_root)) < max(min_new, 1)):
        return 0
    return merge_pending(suite_root, tasks_path)


def merge_pending(suite_root: Path, tasks_path: Path) -> int:
    """Move pending feedback into the tasks file (2 of 3 to train, 1 to val); return the count."""
    queue = Path(suite_root) / FEEDBACK
    if not queue.is_file():
        return 0
    text = read_text(queue)
    rows = _rows(text)
    if not rows:
        return 0
    lines = [line for line in read_text(tasks_path).splitlines() if line.strip()]
    known = {str(json.loads(line).get("id", "")).lower() for line in lines}
    new = [r for r in rows if r["id"].lower() not in known]  # a crash may have merged some already
    if new:
        backup_once(tasks_path)
    for i, row in enumerate(new):
        lines.append(json.dumps({**row, "split": "val" if i % 3 == 2 else "train"}, ensure_ascii=False))
    write_text(tasks_path, "\n".join(lines) + "\n")
    history = Path(suite_root) / MERGED
    logged = {r["id"] for r in _rows(read_text(history))} if history.is_file() else set()
    append_text(history, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if r["id"] not in logged))
    queue.unlink()
    return len(new)


def feedback_footer(suite_root: Path) -> str:
    """Text appended to exported skills so users' judgements are routed back as feedback."""
    return ("## Feedback for automatic improvement\n"
            "When the user judges a result of this skill (good, or wrong with the correct result), "
            f"record it with the wikiskill skill as feedback for the suite `{Path(suite_root)}`.\n")


def export_skills(skills_dir: Path, dest: Path, suite_root: Path) -> list[str]:
    """Copy skills to `dest` with the feedback footer.

    Only files exported by wikiskill before (`export` or this function) are overwritten.
    """
    copied = copy_skills(skills_dir, dest, exported_by_wikiskill, feedback_footer(suite_root))
    return [f"exported {name} -> {target}" if written else f"skip {name}: {target} was not written by wikiskill"
            for name, target, written in copied]
