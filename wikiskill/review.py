"""Hybrid review: a person checks agent outputs once, and the verdicts become reference data.

1. `collect` runs the agent (no skills) on tasks and writes a CSV sheet.
2. A person marks each row OK / NG, writes the corrected answer for NG rows and,
   optionally, a rubric note (what a good answer must contain).
3. `apply_sheet` writes those references into the tasks file (the original is kept
   as <tasks>.bak), after which the evolve loop scores automatically.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .config import Suite
from .rollout import run_tasks
from .scoring import ID_PREFIX, parse_verdict, read_review_rows, strip_id, write_sheet
from .util import read_text, write_text

COLUMNS = ["task_id", "verdict", "corrected_answer", "rubric_note", "prompt", "agent_answer", "output_dir"]


def collect(suite: Suite, backend: Any, splits: List[str], out_dir: Path, workers: int) -> Path:
    tasks = [t for t in suite.tasks if t.split in splits]
    if not tasks:
        raise ValueError(f"no tasks in splits {splits}")
    results = run_tasks(backend, suite, tasks, [], out_dir / "runs", workers)
    sheet = out_dir / "review_sheet.csv"
    write_sheet(sheet, COLUMNS, [
        {"task_id": ID_PREFIX + t.id, "verdict": "", "corrected_answer": "", "rubric_note": "",
         "prompt": t.prompt, "output_dir": results[t.id].workdir,
         "agent_answer": results[t.id].prediction if results[t.id].ok else f"(failed) {results[t.id].error}"}
        for t in tasks])
    return sheet


def apply_sheet(suite: Suite, sheet: Path) -> Dict[str, int]:
    """Merge reviewed references into the suite's tasks file. Returns counts per change type."""
    rows = {strip_id(r.get("task_id", "")): r for r in read_review_rows(sheet)}
    counts = {"answer_from_ok": 0, "answer_from_correction": 0, "rubric_added": 0, "ng_without_correction": 0}
    lines = []
    for line in read_text(suite.tasks_path).splitlines():
        if not line.strip():
            continue
        task = json.loads(line)
        row = rows.get(str(task.get("id")))
        if row is not None:
            _apply_row(task, row, counts)
        lines.append(json.dumps(task, ensure_ascii=False))
    backup = suite.tasks_path.with_name(suite.tasks_path.name + ".bak")
    if not backup.exists():  # keep the original from before the first review
        shutil.copy2(suite.tasks_path, backup)
    write_text(suite.tasks_path, "\n".join(lines) + "\n")
    return counts


def _apply_row(task: Dict[str, Any], row: Dict[str, str], counts: Dict[str, int]) -> None:
    verdict = parse_verdict(row.get("verdict") or "")
    correction = (row.get("corrected_answer") or "").strip()
    note = (row.get("rubric_note") or "").strip()
    agent_answer = (row.get("agent_answer") or "").strip()
    if verdict is not None and verdict >= 1.0 and task.get("answer") is None:
        if agent_answer and not agent_answer.startswith("(failed)"):
            task["answer"] = agent_answer
            counts["answer_from_ok"] += 1
    elif verdict is not None and verdict < 1.0:
        if correction:
            task["answer"] = correction
            counts["answer_from_correction"] += 1
        else:
            counts["ng_without_correction"] += 1
    if note and note not in (task.get("rubric") or ""):
        task["rubric"] = f"{task['rubric']}\n{note}" if task.get("rubric") else note
        counts["rubric_added"] += 1

