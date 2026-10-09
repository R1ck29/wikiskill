"""Task suite loading and validation.

A suite is a directory with `suite.json`, a tasks JSONL file and (optionally) input files.
See docs/SUITE_FORMAT.md for the full format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .util import read_json, read_text

SPLITS = ("train", "val", "test")
SCORER_TYPES = ("exact", "contains", "regex", "number", "script", "llm_judge")
ANSWER_REQUIRED = ("exact", "contains", "regex", "number")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}


class ConfigError(ValueError):
    """The suite or its tasks are invalid."""


@dataclass(frozen=True)
class Task:
    id: str
    split: str
    prompt: str
    answer: Any = None
    files: Tuple[str, ...] = ()
    rubric: Optional[str] = None


@dataclass(frozen=True)
class Suite:
    name: str
    root: Path
    task_description: str
    system_prompt: str
    scorer: dict
    pass_threshold: float
    human_review: bool
    tasks_path: Path
    files_dir: Path
    tasks: Tuple[Task, ...]

    def split(self, name: str) -> List[Task]:
        return [t for t in self.tasks if t.split == name]


def load_suite(
    path: Path, require_answers: bool = True, human_review: Optional[bool] = None
) -> Suite:
    """Load `suite.json` (or a directory containing it) and validate everything.

    Answers are not required when humans review every result (`human_review`) or when
    collecting reference answers with the `review` command (`require_answers=False`).
    `human_review` overrides the suite's own setting (CLI --human-review on/off).
    """
    path = Path(path)
    suite_file = path / "suite.json" if path.is_dir() else path
    if not suite_file.is_file():
        raise ConfigError(f"suite file not found: {suite_file}")
    root = suite_file.parent.resolve()
    cfg = read_json(suite_file)
    if not isinstance(cfg, dict):
        raise ConfigError("suite.json must contain a JSON object")
    scorer = _validate_scorer(cfg.get("scorer"))
    files_dir = (root / cfg.get("files_dir", "files")).resolve()
    tasks_path = (root / _require_str(cfg, "tasks")).resolve()
    if human_review is None:
        human_review = cfg.get("human_review", False)
        if not isinstance(human_review, bool):
            raise ConfigError("suite.json: 'human_review' must be true or false")
    check_answers = require_answers and not human_review
    tasks = load_tasks(tasks_path, files_dir, scorer["type"] if check_answers else None)
    return Suite(
        name=_require_str(cfg, "name"),
        root=root,
        task_description=_require_str(cfg, "task_description"),
        system_prompt=_load_system_prompt(cfg, root),
        scorer=scorer,
        pass_threshold=_number(cfg.get("pass_threshold", 1.0), "pass_threshold"),
        human_review=human_review,
        tasks_path=tasks_path,
        files_dir=files_dir,
        tasks=tasks,
    )


def load_tasks(tasks_path: Path, files_dir: Path, scorer_type: Optional[str]) -> Tuple[Task, ...]:
    """Parse the JSONL tasks file; `scorer_type=None` skips the answer-presence check."""
    if not tasks_path.is_file():
        raise ConfigError(f"tasks file not found: {tasks_path}")
    tasks, seen = [], set()
    for lineno, line in enumerate(read_text(tasks_path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{tasks_path.name}:{lineno}: invalid JSON ({e})") from e
        task = _parse_task(row, f"{tasks_path.name}:{lineno}", files_dir, scorer_type)
        if task.id.lower() in seen:  # ids become file names; macOS/Windows ignore case
            raise ConfigError(f"{tasks_path.name}:{lineno}: duplicate task id {task.id!r} (case-insensitive)")
        seen.add(task.id.lower())
        tasks.append(task)
    if not tasks:
        raise ConfigError(f"{tasks_path} has no tasks")
    return tuple(tasks)


def _parse_task(row: Any, where: str, files_dir: Path, scorer_type: Optional[str]) -> Task:
    if not isinstance(row, dict):
        raise ConfigError(f"{where}: each line must be a JSON object")
    task_id = str(row.get("id", ""))
    if not _ID_RE.fullmatch(task_id) or task_id.split(".")[0].lower() in _WINDOWS_RESERVED:
        raise ConfigError(
            f"{where}: 'id' must match {_ID_RE.pattern} and not be a Windows device name "
            "(it is used as a file name)"
        )
    if row.get("split") not in SPLITS:
        raise ConfigError(f"{where}: 'split' must be one of {SPLITS}")
    if not isinstance(row.get("prompt"), str) or not row["prompt"].strip():
        raise ConfigError(f"{where}: 'prompt' must be a non-empty string")
    files = row.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ConfigError(f"{where}: 'files' must be a list of paths")
    for rel in files:
        target = (files_dir / rel).resolve()
        if files_dir not in target.parents or not target.exists():
            raise ConfigError(f"{where}: file {rel!r} must exist inside {files_dir}")
    if scorer_type in ANSWER_REQUIRED and row.get("answer") is None:
        raise ConfigError(f"{where}: scorer {scorer_type!r} needs an 'answer'")
    if scorer_type == "regex":
        for pattern in row["answer"] if isinstance(row["answer"], list) else [row["answer"]]:
            try:
                re.compile(str(pattern))
            except re.error as e:
                raise ConfigError(f"{where}: invalid regular expression {pattern!r} ({e})") from None
    rubric = row.get("rubric")
    if rubric is not None and not isinstance(rubric, str):
        raise ConfigError(f"{where}: 'rubric' must be a string")
    return Task(task_id, row["split"], row["prompt"], row.get("answer"), tuple(files), rubric)


def _validate_scorer(scorer: Any) -> dict:
    if not isinstance(scorer, dict) or scorer.get("type") not in SCORER_TYPES:
        raise ConfigError(f"'scorer' must be an object whose 'type' is one of {SCORER_TYPES}")
    if scorer["type"] == "script" and not isinstance(scorer.get("path"), str):
        raise ConfigError("scorer 'script' needs a 'path' to a Python file")
    return scorer


def _load_system_prompt(cfg: dict, root: Path) -> str:
    if "system_prompt_file" in cfg:
        prompt_path = root / _require_str(cfg, "system_prompt_file")
        if not prompt_path.is_file():
            raise ConfigError(f"system prompt file not found: {prompt_path}")
        prompt = read_text(prompt_path)
    else:
        prompt = _require_str(cfg, "system_prompt")
    if "{skill_section}" not in prompt:
        raise ConfigError("the system prompt must contain the placeholder {skill_section}")
    return prompt


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"suite.json: '{key}' must be a number")
    return float(value)


def _require_str(cfg: dict, key: str) -> str:
    value = cfg.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"suite.json: '{key}' must be a non-empty string")
    return value
