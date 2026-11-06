"""Automatic train/val/test assignment, so nobody has to split tasks by hand.

The paper prepares fixed splits up front (train:val about 2:1 on every benchmark, Table 6) and holds
out a large test set only to report results. In practice the tasks arrive a few at a time, so:

- every task without a `split` gets one when the suite is loaded, and keeps it (it is written back);
- train and val share the tasks 2:1, separately for each kind of task (`verdict`: good, bad, or an
  example), so the gate also sees the results users rejected and not only the easy ones;
- test (only used by `evaluate`/`compare` on request) takes 1 task in TEST_EVERY once the suite has
  TEST_FROM tasks; before that every task is worth more to learning and to a steadier gate;
- the order inside a batch comes from a hash of the id, not the order the tasks were written in
  (examples are often listed month by month), so the result is the same on every machine.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .util import backup_once, locked, read_text, write_text

VAL_SHARE = 1 / 3  # train:val = 2:1, as in every benchmark of the paper
TEST_FROM = 40  # tasks before the first test task: by then val holds about 13 (the paper's smallest val is 10)
TEST_EVERY = 5  # afterwards 1 task in 5 goes to test
SPLIT_LOCK = "feedback.lock"  # the lock the feedback queue uses too: both rewrite the tasks file


def assign(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    """Return a split for each row of `new`, given the rows that already have one."""
    order = sorted(range(len(new)), key=lambda i: hashlib.sha1(str(new[i].get("id", "")).encode()).hexdigest())
    result = [""] * len(new)
    total = len(existing)
    for i in order:
        total += 1
        if total >= TEST_FROM and total % TEST_EVERY == 0:
            result[i] = "test"
    counts: dict[str, list[int]] = {}  # kind -> [train, val]
    for row in existing:
        if row.get("split") in ("train", "val"):
            counts.setdefault(_kind(row), [0, 0])[row["split"] == "val"] += 1
    for i in order:
        if result[i]:
            continue
        c = counts.setdefault(_kind(new[i]), [0, 0])
        n = c[0] + c[1] + 1
        to_val = n * VAL_SHARE - c[1] > n * (1 - VAL_SHARE) - c[0]  # the split further below its share; ties go to train
        c[to_val] += 1
        result[i] = "val" if to_val else "train"
    _keep_both(existing, result, order)
    return result


def _kind(row: dict[str, Any]) -> str:
    return str(row.get("verdict") or "")


def _keep_both(existing: list[dict[str, Any]], result: list[str], order: list[int]) -> None:
    """Splitting each kind on its own, or the test share, can leave train or val empty (e.g. one good
    and one bad row, or a 40th task added to 39 train tasks); move one new task over so a round can start:
    a test task of this batch first, else one of the other split if that keeps at least one."""
    for missing, other in (("val", "train"), ("train", "val")):
        have = [r.get("split") for r in existing] + result
        if missing in have:
            continue
        movable = [i for i in reversed(order) if result[i] == "test"]
        if not movable and have.count(other) >= 2:
            movable = [i for i in reversed(order) if result[i] == other]
        if movable:
            result[movable[0]] = missing


def fill_missing(tasks_path: Path, lock_dir: Path) -> int:
    """Give every task in `tasks_path` without a split one (written back); return how many.

    A file with a line that is not JSON is left as it is, for the loader to report.
    """
    tasks_path = Path(tasks_path)
    if not tasks_path.is_file() or not any(map(_unsplit, _parse(read_text(tasks_path)) or [])):
        return 0  # checked before taking the lock: most loads have nothing to fill
    with locked(Path(lock_dir) / SPLIT_LOCK):
        lines = _lines(read_text(tasks_path))
        rows = _parse(read_text(tasks_path))
        todo = [i for i, r in enumerate(rows or []) if _unsplit(r)]
        if not rows or not todo:
            return 0
        done = [r for r in rows if isinstance(r, dict) and not _unsplit(r)]
        for i, split in zip(todo, assign(done, [rows[i] for i in todo])):
            lines[i] = json.dumps({**rows[i], "split": split}, ensure_ascii=False)  # other lines stay as written
        backup_once(tasks_path)
        write_text(tasks_path, "\n".join(lines) + "\n")
        return len(todo)


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _parse(text: str) -> list[Any] | None:
    """The rows of a tasks file (one per non-blank line), or None when a line is not JSON (the loader reports that)."""
    try:
        return [json.loads(line) for line in _lines(text)]
    except json.JSONDecodeError:
        return None


def _unsplit(row: Any) -> bool:
    """A task without a split: no key, null or ""."""
    return isinstance(row, dict) and not row.get("split")
