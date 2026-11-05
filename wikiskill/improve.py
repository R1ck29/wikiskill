"""Learning from real usage: queue feedback on real results, then keep evolving the same workspace.

`feedback` appends one judged real-world result to `<suite>/feedback.jsonl` (the pending queue).
`improve` moves pending rows into the train/val tasks once the current round is finished
(`merge_ready`), and the changed task set starts a new round in the same workspace
(EvolveOptions.adopt_new_tasks), so skills, wiki and history carry on.

`improve` also saves its settings to `<suite>/improve.json`; from then on the feedback row that
fills the queue starts the same `improve` as a detached background process (`start_improve`),
so a suite keeps improving without anyone running a command. Deleting improve.json stops that.
Automatic runs use the `sonnet` alias on the claude CLI and a daily budget (AUTO_BUDGET USD unless
`improve --daily-budget` was saved); a spent budget pauses them until the next day. An improve
that fails notes the error in improve.json (`record_failure`), and the next feedback reports it,
as it does a background start that died before the improve command ran (BACKGROUND_ERR).
An automatic run saves the settings it was started with, not its own policy options.
All automatic runs together also stop for the day at AUTO_TOTAL_BUDGET (total_budget).

A suite may also start with no tasks at all (`init --workspace`): its train/val tasks then come only
from feedback, and the first round waits for FIRST_ROUND rows (rows_needed). Like the paper, that
round starts from no skills and no wiki; unlike the paper, its tasks are real requests judged by the user.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .backends import Budget
from .config import ANSWER_REQUIRED, ConfigError, Suite
from .evolve import has_work, is_done
from .skills import copy_skills, exported_by_wikiskill
from .util import (
    append_text,
    backup_once,
    is_windows_reserved,
    locked,
    read_json,
    read_text,
    write_json,
    write_text,
)
from .workspace import Workspace, resolve_workspace

log = logging.getLogger(__name__)
FEEDBACK = "feedback.jsonl"
QUEUE_LOCK = "feedback.lock"  # held while the queue is appended to, read or merged
MERGED = "feedback-merged.jsonl"  # history of rows already moved into the tasks file
AUTO = "improve.json"  # settings of the last improve, reused by the automatic start
BACKGROUND_LOG = "background.log"  # automatic-run output, at the workspace path saved in improve.json
BACKGROUND_ERR = "background.err"  # stderr of the last automatic start until improve opens its log
LOG_LIMIT, LOG_KEEP = 1 << 20, 1 << 18  # a background.log over 1 MB is cut to its last 256 KB
FEEDBACK_FILES = "fb"  # input files of feedback rows: <files_dir>/fb/<id>/<name>
MIN_NEW = 3  # pending feedback rows that start a new round (improve --min-new default)
ITERATIONS = 3  # iterations per improve round (improve --iterations default)
AUTO_MODEL = {"claude": "sonnet"}  # model of automatic runs: the alias resolves on every provider (Bedrock too)
AUTO_BUDGET = 10.0  # USD a day for automatic runs unless `improve --daily-budget` was saved
AUTO_TOTAL_BUDGET = 20.0  # USD a day for all automatic runs of this user together
# the first round of a suite without tasks: 4 train rows, so the Skill Proposer can read the 4 traces
# its rule asks for (paper Appendix E.3, rule 4), and 2 val rows for the gate (merge_pending splits 2:1)
FIRST_ROUND = 6


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
    with locked(suite.root / QUEUE_LOCK):
        queue = suite.root / FEEDBACK
        cut = queue.is_file() and not read_text(queue).endswith("\n")  # a crash cut the last line short
        append_text(queue, ("\n" if cut else "") + json.dumps(row, ensure_ascii=False) + "\n")
        return task_id, len(_queue(suite.root))


def pending(suite_root: Path) -> list[dict[str, Any]]:
    """Feedback rows not yet merged into the tasks file."""
    with locked(Path(suite_root) / QUEUE_LOCK):
        return _queue(suite_root)


def _queue(suite_root: Path) -> list[dict[str, Any]]:
    path = Path(suite_root) / FEEDBACK
    return _rows(read_text(path)) if path.is_file() else []


def _rows(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in filter(str.strip, text.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            rows.append(row)
        else:  # cut off by a crash or a full disk: the rest of the queue still counts
            log.warning("skipping a damaged feedback line: %.80s", line)
    return rows


# ----- improve: rounds ----------------------------------------------------------------
def merge_ready(suite: Suite, state: dict[str, Any] | None, iterations: int, min_new: int) -> int:
    """Merge pending feedback before the first run, or once the round is finished and enough rows wait.

    A suite without train and val tasks waits for FIRST_ROUND rows before its first run.
    """
    if state is None:
        needed = rows_to_start(suite)
    elif is_done(state, iterations):
        needed = max(min_new, 1)
    else:
        return 0
    if len(pending(suite.root)) < needed:
        return 0
    return merge_pending(suite.root, suite.tasks_path)


def startable(suite: Suite) -> bool:
    """True when the suite has the train and val tasks a round needs."""
    return bool(suite.split("train") and suite.split("val"))


def rows_to_start(suite: Suite) -> int:
    """Pending rows the first round of `suite` waits for: enough for both splits once merged
    (row i goes to val when i % 3 == 2, so 3 rows bring the first val task and 1 the first train task)."""
    has_train, has_val = bool(suite.split("train")), bool(suite.split("val"))
    if has_val:
        return 1
    return 3 if has_train else FIRST_ROUND


def rows_needed(suite: Suite, settings: dict[str, Any] | None) -> int:
    """Pending rows that start the next round: FIRST_ROUND before a suite without tasks ever ran."""
    try:
        started = settings is not None and _workspace(settings).load_state() is not None
    except (OSError, ValueError):  # damaged state: count as started, the usual threshold applies
        started = True
    if not started and not startable(suite):
        return rows_to_start(suite)
    return settings["min_new"] if settings else MIN_NEW


def merge_pending(suite_root: Path, tasks_path: Path) -> int:
    """Move pending feedback into the tasks file (2 of 3 to train, 1 to val); return the count."""
    root = Path(suite_root)
    with locked(root / QUEUE_LOCK):  # `feedback` waits meanwhile, so no row is lost
        rows = _queue(root)
        if not rows:
            return 0
        lines = [line for line in read_text(tasks_path).splitlines() if line.strip()]
        known = {str(json.loads(line).get("id", "")).lower() for line in lines}
        new = [r for r in rows if r["id"].lower() not in known]  # a crash may have merged some already
        if new:
            backup_once(tasks_path)
            lines += [json.dumps({**row, "split": "val" if i % 3 == 2 else "train"}, ensure_ascii=False)
                      for i, row in enumerate(new)]
            write_text(tasks_path, "\n".join(lines) + "\n")
        history = root / MERGED
        logged = {r["id"] for r in _rows(read_text(history))} if history.is_file() else set()
        append_text(history, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if r["id"] not in logged))
        (root / FEEDBACK).unlink()
        return len(new)


# ----- automatic start ------------------------------------------------------------------
def save_settings(suite_root: Path, settings: tuple[Path, int, Sequence[str]] | None,
                  keep_error: bool = False) -> None:
    """Remember (workspace, min_new, options) of `improve` for the automatic start; None turns it off.

    A saved last_error is dropped unless `keep_error` (a paused run has not shown the cause is gone).
    """
    path = Path(suite_root) / AUTO
    if settings is None:
        if path.is_file():
            path.unlink()
        return
    workspace, min_new, args = settings
    data = {"workspace": str(Path(workspace).resolve()), "min_new": min_new, "args": list(args)}
    previous = load_settings(suite_root)
    if keep_error and previous and "last_error" in previous:
        data["last_error"] = previous["last_error"]
    if previous != data:
        write_json(path, data)


def load_settings(suite_root: Path) -> dict[str, Any] | None:
    """The saved settings, or None when there are none or they were damaged (then nothing starts)."""
    path = Path(suite_root) / AUTO
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    valid = (isinstance(data, dict) and isinstance(data.get("workspace"), str) and isinstance(data.get("min_new"), int)
             and isinstance(data.get("args"), list) and isinstance(data.get("last_error", {}), dict))
    return data if valid else None


def record_failure(suite: Path, message: str) -> None:
    """Note a failed improve in the saved settings of `suite` (folder or suite.json); the next
    improve that is not paused clears it (save_settings). Never raises: the original error matters more."""
    root = Path(suite) if Path(suite).is_dir() else Path(suite).parent
    settings = load_settings(root)
    if settings is None:
        return
    error = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": message[:500]}
    try:
        write_json(root / AUTO, {**settings, "last_error": error})
    except OSError:
        return


def start_improve(suite: Suite, settings: dict[str, Any]) -> subprocess.Popen:
    """Start the saved `improve` as a detached process that outlives this one (output: background_log).

    It queues behind a running evolve/improve (`--wait`), and exits at once if another one already waits.
    Its stderr goes to a fresh BACKGROUND_ERR, which stays empty once the command opens its own log:
    only a start that dies before that (a broken interpreter or install) writes there (background_error).
    """
    log_path = background_log(settings)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    trim_log(log_path)
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join(p for p in (package_root, os.environ.get("PYTHONPATH")) if p))
    options = [item for pair in automatic_options(settings).items() for item in pair]
    command = [sys.executable, "-m", "wikiskill", "improve", str(suite.root), "--workspace", settings["workspace"],
               "--min-new", str(settings["min_new"]), *options, "--wait", "--automatic", "--log", str(log_path)]
    # Windows: a new hidden console (not DETACHED_PROCESS, which makes every CLI call open a console window)
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(log_path, "a", encoding="utf-8") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} started by feedback ===\n")
    with open(log_path.with_name(BACKGROUND_ERR), "wb") as err:  # the child keeps its own handle
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                env=env, cwd=package_root, start_new_session=os.name != "nt", creationflags=flags)


def automatic_options(settings: dict[str, Any]) -> dict[str, str]:
    """The saved options with the policy of automatic runs: AUTO_MODEL, and AUTO_BUDGET unless one was saved."""
    args = [str(a) for a in settings["args"]]
    options = dict(zip(args[::2], args[1::2]))  # saved as --flag value pairs (_improve_args in cli.py)
    model = AUTO_MODEL.get(options.get("--backend", "claude"))
    if model:
        options["--model"] = model
    try:
        budget = float(options["--daily-budget"])
    except (KeyError, ValueError):
        budget = math.nan
    if not (math.isfinite(budget) and budget > 0):  # none saved, or a hand edit gone wrong
        options["--daily-budget"] = str(AUTO_BUDGET)
    return options


def _workspace(settings: dict[str, Any]) -> Workspace:
    """The workspace `improve` actually runs in for this saved path.

    Same rule as the improve command: a 0.2.x layout keeps using its newest gen-NNN folder.
    `background.log` stays at the saved path, because that is the file passed to `--log`.
    """
    return resolve_workspace(Path(settings["workspace"]))


def unfinished_round(settings: dict[str, Any], suite: Suite) -> bool:
    """True when the saved workspace stopped inside a round (paused, interrupted or failed): the next
    feedback row starts it again even with fewer than min_new rows waiting. A workspace without state
    whose suite can start counts too: an improve stopped after merging rows but before the round began."""
    try:
        state = _workspace(settings).load_state()
        iterations = int(automatic_options(settings).get("--iterations", ITERATIONS))
        return startable(suite) if state is None else has_work(state, suite, iterations)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):  # damaged state: rows still count as usual
        return False


def automatic_budget(settings: dict[str, Any]) -> Budget:
    """The budget the automatic run would spend from (the ledger lives in its workspace)."""
    limit = float(automatic_options(settings)["--daily-budget"])
    return Budget(_workspace(settings).spend_path, limit, total_budget())


def work_root() -> Path:
    """The folder that holds the users' suites and workspaces (`list` looks here)."""
    return Path.home() / "wikiskill-work"


def total_budget() -> Budget:
    """The shared budget of all automatic runs: AUTO_TOTAL_BUDGET a day, ledger in work_root()."""
    return Budget(work_root() / "spend-total.json", AUTO_TOTAL_BUDGET, label="the daily total of automatic runs")


def background_log(settings: dict[str, Any]) -> Path:
    return Path(settings["workspace"]) / BACKGROUND_LOG


def background_error(settings: dict[str, Any]) -> str:
    """The last line the last automatic start wrote before its improve command ran, or ""."""
    try:
        lines = Path(settings["workspace"], BACKGROUND_ERR).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ""
    return next((line.strip()[:500] for line in reversed(lines) if line.strip()), "")


def trim_log(path: Path, limit: int = LOG_LIMIT, keep: int = LOG_KEEP) -> None:
    """Keep only the whole lines in the last `keep` bytes of a log that grew over `limit`.

    Rewritten in place, not replaced: a run that still appends to it (`improve --log`) keeps
    the same file and goes on writing at its new end.
    """
    try:
        if path.stat().st_size <= limit:
            return
        with open(path, "rb") as f:
            f.seek(-(keep + 1), os.SEEK_END)  # one byte more: is the cut already at a line start?
            tail = f.read()
        cut = tail.find(b"\n")
        path.write_bytes(tail[cut + 1:] if cut >= 0 else b"")
    except OSError:  # no log yet, or it cannot be written now: the start goes ahead either way
        return


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
