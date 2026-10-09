"""Evaluate a skill set on a split, and compare two evaluations with a paired bootstrap.

The bootstrap follows the paper's Appendix C: resample tasks with replacement
(1,000 iterations), recompute both means on the same resample, and report how often
the difference does not favour the observed winner (a one-sided p-value).
"""
from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Suite
from .rollout import run_tasks
from .scoring import Scorer
from .skills import load_skills
from .util import read_json, write_json

log = logging.getLogger(__name__)


def evaluate(suite: Suite, backend: Any, skills_dir: Optional[Path], split: str, out_dir: Path,
             workers: int, judge: Any = None) -> Dict[str, Any]:
    """Run `split` with the skills in `skills_dir` (None = no skills) and write report.json."""
    tasks = suite.split(split)
    if not tasks:
        raise ValueError(f"the suite has no '{split}' tasks")
    skills = load_skills(skills_dir) if skills_dir else []
    results = run_tasks(backend, suite, tasks, skills, out_dir / "runs", workers)
    outcomes = Scorer(suite, judge or backend).score_all(tasks, results, out_dir / "review.csv")
    scores = {t.id: outcomes[t.id].score for t in tasks}
    report = {
        "suite": suite.name, "split": split, "backend": getattr(backend, "name", "?"),
        "skills": [name for name, _ in skills], "skills_dir": str(skills_dir) if skills_dir else None,
        "mean_score": sum(scores.values()) / len(scores), "n_tasks": len(scores), "scores": scores,
        "notes": {tid: o.note for tid, o in outcomes.items() if o.note},
    }
    write_json(out_dir / "report.json", report)
    return report


def paired_bootstrap(a: Dict[str, float], b: Dict[str, float], iterations: int = 1000,
                     seed: int = 0) -> Dict[str, Any]:
    """Compare per-task scores of two reports on their shared tasks (b minus a)."""
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    shared = sorted(set(a) & set(b))
    if not shared:
        raise ValueError("the two reports share no task ids")
    pairs: List[Tuple[float, float]] = [(a[t], b[t]) for t in shared]
    observed = sum(y - x for x, y in pairs) / len(pairs)
    rng = random.Random(seed)
    diffs = []
    for _ in range(iterations):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        diffs.append(sum(y - x for x, y in sample) / len(sample))
    diffs.sort()
    against = sum(1 for d in diffs if (d <= 0 if observed > 0 else d >= 0)) / iterations
    return {
        "n_shared_tasks": len(shared),
        "mean_a": sum(x for x, _ in pairs) / len(pairs),
        "mean_b": sum(y for _, y in pairs) / len(pairs),
        "difference_b_minus_a": observed,
        "ci95": [diffs[int(0.025 * iterations)], diffs[min(int(0.975 * iterations), iterations - 1)]],
        "p_value": against if observed != 0 else 1.0,
        "significant_at_0.05": observed != 0 and against < 0.05,
    }


def compare_reports(path_a: Path, path_b: Path, iterations: int = 1000, seed: int = 0) -> Dict[str, Any]:
    return paired_bootstrap(read_json(path_a)["scores"], read_json(path_b)["scores"], iterations, seed)
