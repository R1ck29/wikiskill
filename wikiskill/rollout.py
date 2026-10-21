"""Inference Agent rollouts: run every task of a split with the current skills (paper §3.2.1).

Each task runs in a fresh temporary directory outside the workspace, so the agent
sees its input files and the skills in its system prompt but never the wiki
(the paper's ablation, Table 3, shows wiki access during rollouts hurts).
Finished runs are cached under runs/<stage>/<task_id>/ so an interrupted run resumes.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backends import kill_all_running, reset_stop
from .config import Suite, Task
from .scoring import Outcome, extract_answer
from .skills import render_skill_section
from .util import read_json, read_text, write_json, write_text

log = logging.getLogger(__name__)


class RolloutInfraError(RuntimeError):
    """Some runs failed because the CLI itself failed; rerun to retry just those tasks."""


@dataclass(frozen=True)
class RolloutResult:
    task_id: str
    final_text: str
    prediction: str
    ok: bool
    error: str
    workdir: str
    files: tuple[str, ...]
    trace: str = ""
    infra_error: bool = False


def system_prompt(suite: Suite, skills: list[tuple[str, str]]) -> str:
    return suite.system_prompt.replace("{skill_section}", render_skill_section(skills))


def user_prompt(task: Task) -> str:
    if not task.files:
        return task.prompt
    listing = "\n".join(f"- {name}" for name in task.files)
    return f"{task.prompt}\n\nInput files in your working directory:\n{listing}"


def cache_key(backend: Any, suite: Suite, task: Task, system: str) -> str:
    """Identifies a run: backend + model, system prompt (skills), task text and input file contents."""
    digest = hashlib.sha256()
    for part in (getattr(backend, "name", "?"), system, user_prompt(task)):
        digest.update(part.encode("utf-8") + b"\0")
    for rel in task.files:
        src = suite.files_dir / rel
        for path in sorted(src.rglob("*")) if src.is_dir() else [src]:
            if path.is_file():
                digest.update(path.relative_to(suite.files_dir).as_posix().encode("utf-8") + path.read_bytes())
    return digest.hexdigest()


def run_task(backend: Any, suite: Suite, task: Task, system: str, out_dir: Path) -> RolloutResult:
    """Run one task (or load its cached result)."""
    cached = out_dir / "result.json"
    key = cache_key(backend, suite, task, system)
    if cached.is_file():
        data = read_json(cached)
        if data.pop("cache_key", None) == key:  # same model, prompt, skills and inputs: reuse the run
            data.update(files=tuple(data["files"]), workdir=str(out_dir / "work"))
            return RolloutResult(**data, trace=read_text(out_dir / "trace.md"))
        cached.unlink()  # different skills or task text: run again
    tmp = Path(tempfile.mkdtemp(prefix="wikiskill-task-"))
    try:
        for rel in task.files:
            src, dst = suite.files_dir / rel, tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        agent = backend.run_agent(user_prompt(task), system, tmp)
        workdir = out_dir / "work"
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(tmp, workdir, symlinks=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    files = tuple(sorted(p.relative_to(workdir).as_posix() for p in workdir.rglob("*") if p.is_file()))
    result = RolloutResult(task.id, agent.final_text, extract_answer(agent.final_text),
                           agent.ok, agent.error, str(workdir), files, agent.trace, agent.infra_error)
    if agent.infra_error:  # not cached: the next run retries this task
        return result
    write_text(out_dir / "trace.md", agent.trace)
    write_json(cached, {**{k: v for k, v in asdict(result).items() if k != "trace"}, "cache_key": key})
    return result


def run_tasks(backend: Any, suite: Suite, tasks: list[Task], skills: list[tuple[str, str]],
              stage_dir: Path, workers: int) -> dict[str, RolloutResult]:
    """Run tasks in parallel; returns {task_id: result} in task order."""
    system = system_prompt(suite, skills)
    done: dict[str, RolloutResult] = {}

    def one(task: Task) -> RolloutResult:
        result = run_task(backend, suite, task, system, stage_dir / task.id)
        done[task.id] = result
        log.info("  [%s] %d/%d %s%s", stage_dir.name, len(done), len(tasks), task.id,
                 "" if result.ok else f" (failed: {result.error[:120]})")
        return result

    reset_stop()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [pool.submit(one, task) for task in tasks]
    try:
        pending = set(futures)
        while pending:  # short waits keep Ctrl+C responsive (also on Windows)
            done_now, pending = wait(pending, timeout=1.0, return_when=FIRST_EXCEPTION)
            for future in done_now:
                future.result()  # re-raise a worker's exception right away
    except BaseException:
        for future in futures:
            future.cancel()
        kill_all_running()  # running CLIs are killed and no worker may start or retry one
        pool.shutdown(wait=True)  # returns quickly; the workspace lock is released only after this
        raise
    pool.shutdown(wait=True)
    results = [f.result() for f in futures]
    failed = [r.task_id for r in results if r.infra_error]
    if failed:  # never score an outage: it would mislead the wiki and the validation gate
        raise RolloutInfraError(
            f"{len(failed)} task(s) failed because the CLI failed ({', '.join(failed[:10])}); "
            "completed tasks are cached, so run the same command again to retry only these")
    return {r.task_id: r for r in results}


def format_trace(task: Task, result: RolloutResult, outcome: Outcome) -> str:
    """The Raw Layer document for one training task: outcome header + full trajectory."""
    expected = "(none: judged by a reviewer or rubric)" if task.answer is None else str(task.answer)
    lines = [
        f"# Trace: {task.id}",
        "",
        f"- Outcome: {'PASS' if outcome.passed else 'FAIL'} (score {outcome.score:g})",
        f"- Expected answer: {expected}",
        f"- Agent answer: {result.prediction or '(empty)'}",
    ]
    if outcome.note:
        lines.append(f"- Scoring note: {outcome.note}")
    if not result.ok:
        lines.append(f"- Agent run failed: {result.error}")
    lines += ["", "## Task", "", user_prompt(task), "", "## Trajectory", "", result.trace or "(no trace)",
              "", "## Final response", "", result.final_text or "(empty)", ""]
    return "\n".join(lines)
