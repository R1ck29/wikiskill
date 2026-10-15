"""Learning from real usage: queue feedback on real results, then re-evolve in generations.

`feedback` appends one judged real-world result to `<suite>/feedback.jsonl` (the pending queue).
`improve` (see `prepare_generation`) turns pending rows into train/val tasks and evolves a new generation
`gen-NNN` that starts from the previous generation's skills and wiki, so improvement continues
instead of starting over (a new generation is needed because train/val changed).
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import ANSWER_REQUIRED, ConfigError, Suite
from .skills import load_skills
from .util import append_text, is_windows_reserved, read_text, write_text
from .workspace import Workspace

FEEDBACK = "feedback.jsonl"
MERGED = "feedback-merged.jsonl"  # history of rows already moved into the tasks file
FEEDBACK_FILES = "fb"  # input files of feedback rows: <files_dir>/fb/<id>/<name>
FOOTER_MARK = "<!-- wikiskill:feedback -->"
STAGING = "gen-next"  # a generation is prepared here, then renamed to gen-NNN


def feedback_row(scorer_type: str, prompt: str, verdict: str, output: Optional[str] = None,
                 correct: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
    """Build the task fields (without id/files/split) for one judged real result."""
    if not prompt.strip():
        raise ConfigError("feedback needs the request text (--prompt)")
    if verdict == "good":
        if not output:
            raise ConfigError("a 'good' verdict needs the accepted result (--output)")
        row: Dict[str, Any] = {"prompt": prompt, "answer": output}
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


def record_feedback(suite: Suite, prompt: str, verdict: str, output: Optional[str] = None,
                    correct: Optional[str] = None, note: Optional[str] = None,
                    files: Sequence[Path] = ()) -> Tuple[str, int]:
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
    row = {"id": task_id, **row, **({"files": rel} if rel else {})}
    append_text(suite.root / FEEDBACK, json.dumps(row, ensure_ascii=False) + "\n")
    return task_id, len(pending(suite.root))


def pending(suite_root: Path) -> List[Dict[str, Any]]:
    """Feedback rows not yet merged into the tasks file."""
    path = Path(suite_root) / FEEDBACK
    if not path.is_file():
        return []
    return [json.loads(line) for line in read_text(path).splitlines() if line.strip()]


# ----- improve: generations ----------------------------------------------------------
def generations(root: Path) -> List[Path]:
    return sorted(p for p in Path(root).glob("gen-[0-9][0-9][0-9]") if p.is_dir())


def finished(state: Optional[Dict[str, Any]], iterations: int) -> bool:
    """True when `evolve` with this many iterations has nothing left to do (mirrors Evolution.run)."""
    if state is None or state["r_best"] is None:
        return False
    return state["iteration"] > iterations or state["r_best"] >= 1.0


def prepare_generation(suite_root: Path, tasks_path: Path, root: Path, iterations: int,
                       min_new: int) -> Tuple[Optional[Workspace], int]:
    """Pick the workspace `improve` should evolve: (workspace or None if nothing to do, merged rows).

    An unfinished generation is resumed as is. Otherwise a new generation is started from the
    previous one's skills and wiki, with the pending feedback merged into the tasks file.
    """
    gens = generations(root)
    last = Workspace(gens[-1]) if gens else None
    if last is not None and not finished(last.load_state(), iterations):
        return last, 0
    staging = Path(root) / STAGING  # left over only when a previous preparation was interrupted
    if last is not None and not staging.exists() and len(pending(suite_root)) < max(min_new, 1):
        return None, 0
    if staging.exists():
        shutil.rmtree(str(staging))
    ws = Workspace(staging)
    ws.init()
    if last is not None:  # carry the knowledge over; raw traces stay with their generation
        for layer in ("skills", "wiki"):
            shutil.copytree(str(last.root / layer), str(ws.root / layer), dirs_exist_ok=True)
    merged = merge_pending(suite_root, tasks_path)
    number = int(gens[-1].name[4:]) + 1 if gens else 1
    final = Path(root) / f"gen-{number:03d}"
    staging.rename(final)  # the generation appears only once it is complete
    return Workspace(final), merged


def merge_pending(suite_root: Path, tasks_path: Path) -> int:
    """Move pending feedback into the tasks file (2 of 3 to train, 1 to val); return the count."""
    rows = pending(suite_root)
    if not rows:
        return 0
    lines = [line for line in read_text(tasks_path).splitlines() if line.strip()]
    known = {str(json.loads(line).get("id", "")).lower() for line in lines}
    new = [r for r in rows if r["id"].lower() not in known]  # a crash may have merged some already
    backup = tasks_path.with_name(tasks_path.name + ".bak")
    if new and not backup.exists():
        shutil.copy2(str(tasks_path), str(backup))
    for i, row in enumerate(new):
        lines.append(json.dumps({**row, "split": "val" if i % 3 == 2 else "train"}, ensure_ascii=False))
    write_text(tasks_path, "\n".join(lines) + "\n")
    append_text(Path(suite_root) / MERGED, read_text(Path(suite_root) / FEEDBACK))
    (Path(suite_root) / FEEDBACK).unlink()
    return len(new)


def export_skills(skills_dir: Path, dest: Path, suite_root: Path) -> List[str]:
    """Copy skills to `dest` with a footer that routes users' judgements back as feedback.

    Only files this function wrote before (they carry FOOTER_MARK) are overwritten.
    """
    footer = (f"\n\n{FOOTER_MARK}\n## Feedback for automatic improvement\n"
              "When the user judges a result of this skill (good, or wrong with the correct result), "
              f"record it with the wikiskill skill as feedback for the suite `{Path(suite_root)}`.\n")
    messages = []
    for name, text in load_skills(skills_dir):
        target = Path(dest).expanduser() / name / "SKILL.md"
        if target.exists() and FOOTER_MARK not in read_text(target):
            messages.append(f"skip {name}: {target} was not written by wikiskill")
            continue
        write_text(target, text.rstrip("\n") + footer)
        messages.append(f"exported {name} -> {target}")
    return messages
