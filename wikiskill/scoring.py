"""Scoring f(prediction, answer) in [0, 1] (paper §2), automatic or by a human reviewer.

Automatic scorers: exact, contains, regex, number, script, llm_judge.
Human review: results are written to a CSV sheet; a person fills in `verdict`
(OK / NG / a number 0-1) and optionally `comment`, then the run is resumed.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import Suite, Task
from .util import csv_cell, csv_value, extract_json_object, truncate

if TYPE_CHECKING:
    from .rollout import RolloutResult

_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.DOTALL | re.IGNORECASE)
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
ID_PREFIX = "id:"  # keeps Excel from turning ids like 001 or 2026-01 into numbers or dates
OK_WORDS = {"ok", "pass", "yes", "1", "○", "〇", "合格", "正解"}
NG_WORDS = {"ng", "fail", "no", "0", "×", "✕", "要修正", "修正", "不合格", "不正解"}
REVIEW_COLUMNS = ["task_id", "verdict", "comment", "prompt", "reference", "agent_answer", "auto_score",
                  "output_dir", "answer_key"]
TEXT_COLUMNS = ("comment", "prompt", "reference", "agent_answer", "corrected_answer", "rubric_note")


class HumanReviewPending(Exception):
    """Raised when a review sheet must be filled in before the run can continue."""

    def __init__(self, sheet: Path, missing: list[str]) -> None:
        super().__init__(f"human review needed: fill in {sheet} ({len(missing)} rows open)")
        self.sheet, self.missing = sheet, missing


@dataclass(frozen=True)
class Outcome:
    task_id: str
    score: float
    passed: bool
    prediction: str
    note: str = ""


def extract_answer(text: str) -> str:
    """The last <answer>...</answer> block, or the whole text when there is none."""
    matches = _ANSWER_RE.findall(text or "")
    return (matches[-1] if matches else text or "").strip()


def normalize(text: Any) -> str:
    """Unicode NFKC (full-width -> half-width), case-fold, collapse whitespace."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def parse_number(text: Any) -> float | None:
    text = normalize(text).replace(",", "").replace("\u2212", "-")  # U+2212 minus sign
    match = _NUMBER_RE.search(text)
    return float(match.group()) if match else None


def _answers(task: Task) -> list[Any]:
    return task.answer if isinstance(task.answer, list) else [task.answer]


def score_exact(task: Task, pred: str, cfg: dict) -> float:
    return float(any(normalize(pred) == normalize(a) for a in _answers(task)))


def score_contains(task: Task, pred: str, cfg: dict) -> float:
    return float(any(normalize(a) in normalize(pred) for a in _answers(task)))


def score_regex(task: Task, pred: str, cfg: dict) -> float:  # patterns are checked by config.load_suite
    return float(any(re.search(str(a), pred, re.IGNORECASE) for a in _answers(task)))


def score_number(task: Task, pred: str, cfg: dict) -> float:
    value = parse_number(pred)
    if value is None:
        return 0.0
    rel, abs_ = float(cfg.get("rel_tol", 1e-6)), float(cfg.get("abs_tol", 1e-6))
    return float(any(_close(value, parse_number(a), rel, abs_) for a in _answers(task)))


def _close(value: float, target: float | None, rel: float, abs_: float) -> bool:
    return target is not None and math.isclose(value, target, rel_tol=rel, abs_tol=abs_)


SIMPLE_SCORERS: dict[str, Callable[[Task, str, dict], float]] = {
    "exact": score_exact, "contains": score_contains, "regex": score_regex, "number": score_number,
}

JUDGE_SYSTEM = (
    "You are a strict evaluator. Grade how well the agent's response completes the task, "
    "using the reference answer and rubric when given. Reply with only a JSON object: "
    '{"score": <number between 0 and 1>, "reason": "<one or two sentences>"}'
)


class Scorer:
    """Scores rollout results for one suite. `judge` is any object with complete(prompt, system)."""

    def __init__(self, suite: Suite, judge: Any = None) -> None:
        self.suite, self.cfg, self.judge = suite, suite.scorer, judge
        self._script: Callable | None = None

    def auto(self, task: Task, result: RolloutResult) -> tuple[float, str]:
        if not result.ok:
            return 0.0, f"agent run failed: {result.error}"
        kind, pred = self.cfg["type"], result.prediction
        if kind in SIMPLE_SCORERS:
            return SIMPLE_SCORERS[kind](task, pred, self.cfg), ""
        if kind == "script":
            return self._score_script(task, result)
        return self._score_judge(task, result)

    def _score_script(self, task: Task, result: RolloutResult) -> tuple[float, str]:
        try:
            return self._call_script(task, result)
        except Exception as e:  # noqa: BLE001 -- a scorer bug or a missing output file must not stop the run
            return 0.0, f"scorer script error: {type(e).__name__}: {e}"

    def _call_script(self, task: Task, result: RolloutResult) -> tuple[float, str]:
        if self._script is None:
            path = (self.suite.root / self.cfg["path"]).resolve()
            spec = importlib.util.spec_from_file_location("wikiskill_user_scorer", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # the user's own scorer file
            self._script = module.score
        task_dict = {"id": task.id, "prompt": task.prompt, "answer": task.answer, "rubric": task.rubric}
        value = self._script(task_dict, result.prediction, str(result.workdir))
        score, note = value if isinstance(value, tuple) else (value, "")
        return _clamp(score), str(note)

    def _score_judge(self, task: Task, result: RolloutResult) -> tuple[float, str]:
        if self.judge is None:
            raise ValueError("llm_judge scorer needs a judge backend")
        rubric = "\n".join(r for r in (self.cfg.get("rubric", ""), task.rubric or "") if r)  # shared + per task
        prompt = (
            f"## Task\n{task.prompt}\n\n## Reference answer\n{task.answer if task.answer is not None else '(none)'}"
            f"\n\n## Rubric\n{rubric or '(none)'}\n\n## Agent response\n{truncate(result.final_text, 15000)}"
            f"\n\n## Files in the agent's working directory\n{', '.join(result.files) or '(none)'}"
        )
        try:
            verdict = extract_json_object(self.judge.complete(prompt, system=JUDGE_SYSTEM))
            return _clamp(float(verdict["score"])), str(verdict.get("reason", ""))
        except (ValueError, KeyError, TypeError) as e:
            return 0.0, f"judge output unusable: {e}"

    # ----- whole result sets ---------------------------------------------------------
    def score_all(self, tasks: list[Task], results: dict[str, RolloutResult],
                  sheet: Path | None = None) -> dict[str, Outcome]:
        """Automatic scores, or human verdicts from `sheet` when the suite uses human review."""
        if self.suite.human_review:
            if sheet is None:
                raise ValueError("human review needs a review sheet path")
            scored = self._human_verdicts(tasks, results, sheet)
        else:
            scored = {t.id: self.auto(t, results[t.id]) for t in tasks}
        return {
            t.id: Outcome(t.id, s, s >= self.suite.pass_threshold, results[t.id].prediction, note)
            for t in tasks for s, note in [scored[t.id]]
        }

    def _human_verdicts(self, tasks: list[Task], results: dict[str, RolloutResult],
                        sheet: Path) -> dict[str, tuple[float, str]]:
        """Read verdicts; write the sheet (or add rows for tasks it lacks) and pause if any are open."""
        rows = read_review_rows(sheet) if sheet.is_file() else []
        current = {t.id: self._sheet_row(t, results[t.id]) for t in tasks}
        kept = [r for r in rows if strip_id(r.get("task_id", "")) not in current
                or r.get("answer_key") == current[strip_id(r["task_id"])]["answer_key"]]
        known = {strip_id(r.get("task_id", "")) for r in kept}
        new_rows = [row for tid, row in current.items() if tid not in known]  # new or re-run tasks
        if new_rows or len(kept) != len(rows):
            write_review_sheet(sheet, kept + new_rows)
        verdicts = read_review_sheet(sheet)
        missing = [t.id for t in tasks if t.id not in verdicts]
        if missing:
            raise HumanReviewPending(sheet, missing)
        return {t.id: verdicts[t.id] for t in tasks}

    def _sheet_row(self, task: Task, result: RolloutResult) -> dict[str, str]:
        hint = ""
        if task.answer is not None and self.cfg["type"] in SIMPLE_SCORERS and result.ok:
            hint = f"{self.auto(task, result)[0]:g}"
        return {
            "task_id": ID_PREFIX + task.id, "verdict": "", "comment": "", "prompt": task.prompt,
            "reference": "" if task.answer is None else str(task.answer),
            "agent_answer": result.prediction if result.ok else f"(failed) {result.error}",
            "auto_score": hint, "output_dir": str(result.workdir), "answer_key": answer_key(result),
        }


def answer_key(result: RolloutResult) -> str:
    """Short hash of the reviewed output: a verdict only counts for the answer it was given for."""
    text = f"{result.ok}\0{result.final_text}\0{','.join(result.files)}"
    return "h:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clamp(value: Any) -> float:
    value = float(value)
    return 0.0 if math.isnan(value) else min(max(value, 0.0), 1.0)


def strip_id(value: str) -> str:
    value = (value or "").strip()
    return value.removeprefix(ID_PREFIX)


def parse_verdict(value: str) -> float | None:
    """OK / NG words (English or Japanese) or a number in [0, 1]; None when blank."""
    text = normalize(value)
    if not text:
        return None
    if text in OK_WORDS:
        return 1.0
    if text in NG_WORDS:
        return 0.0
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"unrecognised verdict {value!r}; use OK, NG or a number 0-1") from None
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"verdict {value!r} must be between 0 and 1")
    return number


def write_review_sheet(path: Path, rows: list[dict[str, str]]) -> None:
    """UTF-8 with BOM so Excel on Windows shows Japanese correctly."""
    write_sheet(path, REVIEW_COLUMNS, rows)


def write_sheet(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Write a CSV for Excel: UTF-8 with BOM, and text cells that look like formulas are escaped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_cell(str(v)) if k in TEXT_COLUMNS and v else v for k, v in row.items()})


def read_review_rows(path: Path) -> list[dict[str, str]]:
    """Read a review sheet saved as UTF-8 (with or without BOM) or as Excel's Shift_JIS (cp932)."""
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path}: save the sheet as CSV UTF-8 or CSV (Shift_JIS)")
    rows = [{k: csv_value(v) if k in TEXT_COLUMNS and isinstance(v, str) else v for k, v in row.items()}
            for row in csv.DictReader(io.StringIO(text, newline=""))]
    if rows and ("task_id" not in rows[0] or "verdict" not in rows[0]):
        raise ValueError(f"{path}: the columns task_id and verdict are required")
    return rows


def read_review_sheet(path: Path) -> dict[str, tuple[float, str]]:
    """{task_id: (score, comment)} for every row whose verdict is filled in."""
    verdicts = {}
    for row in read_review_rows(path):
        try:
            score = parse_verdict(row.get("verdict") or "")
        except ValueError as e:
            raise ValueError(f"{path}, task {row.get('task_id')}: {e}") from None
        if score is not None:
            comment = (row.get("comment") or "").strip()
            verdicts[strip_id(row["task_id"])] = (score, f"reviewer: {comment}" if comment else "reviewer verdict")
    return verdicts
