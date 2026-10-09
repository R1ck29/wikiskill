"""The WikiSkill evolution loop (paper Algorithm 1).

    baseline: R_best <- R(val rollouts with no skills)
    for k = 1..K (stop early when R_best = 1.0):
        rollout   train rollouts with S_{k-1}; scored traces -> raw/iter_k   (Raw Layer)
        maintain  Wiki Maintainer consolidates sampled traces into wiki/  (Wiki Layer)
        propose   Skill Proposer -> one atomic proposal P_k
        validate  S'_k = Apply(S_{k-1}, P_k); score on val
        commit    accept iff R(val) > R_best, else roll back skills only;
                  append the outcome and diff to wiki/skill-impact.md

Every stage stores its output before the next starts, so `evolve` can be re-run to
resume after an interruption or after a human review sheet has been filled in.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Suite, Task
from .maintainer import run_maintainer, sample_traces
from .proposer import run_proposer
from .rollout import format_trace, run_tasks
from .scoring import Outcome, Scorer
from .skills import apply_proposal, load_skills
from .util import append_text, read_json, read_text, write_json
from .workspace import Workspace

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvolveOptions:
    iterations: int = 8  # the paper reports accepted updates up to iteration 7 of 0-7 (Table 5)
    workers: int = 4
    seed: int = 0
    max_turns: int = 20  # paper: 10 <= T_ReAct <= 20 (Appendix D.2)
    min_traces: int = 4  # proposer rule 4 (Appendix E.3)


def mean(outcomes: Dict[str, Outcome]) -> float:
    return sum(o.score for o in outcomes.values()) / len(outcomes) if outcomes else 0.0


class Evolution:
    def __init__(self, suite: Suite, backend: Any, ws: Workspace, opts: EvolveOptions,
                 judge: Any = None) -> None:
        if not suite.split("train") or not suite.split("val"):
            raise ValueError("evolve needs at least one 'train' and one 'val' task")
        self.suite, self.backend, self.ws, self.opts = suite, backend, ws, opts
        self.scorer = Scorer(suite, judge or backend)

    # ----- driver ------------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        self.ws.init()
        state = self._load_state()
        if state["r_best"] is None:
            outcomes = self._score_split("val", load_skills(self.ws.skills), "baseline_val",
                                         self.ws.stage_dir("baseline_val"))
            state.update(r_best=mean(outcomes), baseline=mean(outcomes))
            self.ws.save_state(state)
            log.info("baseline validation score (no skills): %.4f", state["r_best"])
        while state["iteration"] <= self.opts.iterations:
            if state["r_best"] >= 1.0:
                log.info("validation score is 1.0; stopping early (Algorithm 1, line 4)")
                break
            self._iteration(state)
        return state

    def _load_state(self) -> Dict[str, Any]:
        state = self.ws.load_state()
        if state is None:
            state = {"suite": self.suite.name, "backend": getattr(self.backend, "name", "?"),
                     "tasks": task_fingerprint(self.suite), "baseline": None, "r_best": None,
                     "iteration": 1, "stage": "rollout", "pending": None, "history": []}
            self.ws.save_state(state)
        elif state["suite"] != self.suite.name:
            raise ValueError(f"workspace belongs to suite {state['suite']!r}, not {self.suite.name!r}")
        elif "tasks" not in state:  # written by an older version: adopt the current task set
            state["tasks"] = task_fingerprint(self.suite)
            self.ws.save_state(state)
        elif state["tasks"] != task_fingerprint(self.suite):
            raise ValueError("the train/val task ids changed since this workspace was started; scores would "
                             "not be comparable. Use a new --workspace (answers and rubrics may change freely).")
        return state

    def _iteration(self, state: Dict[str, Any]) -> None:
        k = state["iteration"]
        log.info("=== iteration %d/%d (stage: %s, best val %.4f) ===", k, self.opts.iterations,
                 state["stage"], state["r_best"])
        outcomes = self._train_outcomes(k)
        tasks = self.suite.split("train")
        if state["stage"] == "rollout":
            self._advance(state, "maintain")
        if state["stage"] == "maintain":
            self._maintain(k, outcomes)
            self._advance(state, "propose")
        if state["stage"] == "propose":
            proposal_path = self._iter_dir(k) / "proposal.json"
            if not proposal_path.is_file():
                write_json(proposal_path, run_proposer(self.backend, self.ws, self.suite, k, tasks, outcomes,
                                                       self.opts.max_turns, self.opts.min_traces))
            self._advance(state, "validate")
        if state["stage"] == "validate":
            state["pending"] = self._validate(k, state["r_best"])
            self._advance(state, "commit")
        self._commit(state)

    def _advance(self, state: Dict[str, Any], stage: str) -> None:
        state["stage"] = stage
        self.ws.save_state(state)

    def _iter_dir(self, k: int) -> Path:
        return self.ws.runs / f"iter_{k:03d}"

    # ----- stages ------------------------------------------------------------------
    def _train_outcomes(self, k: int) -> Dict[str, Outcome]:
        """Rollout stage: score the training split and freeze it in the Raw Layer."""
        raw = self.ws.raw_dir(k)
        if raw.is_dir():
            return {tid: Outcome(**o) for tid, o in read_json(raw / "summary.json").items()}
        tasks = self.suite.split("train")
        results = run_tasks(self.backend, self.suite, tasks, load_skills(self.ws.skills),
                            self._iter_dir(k) / "train", self.opts.workers)
        outcomes = self.scorer.score_all(tasks, results, self.ws.reviews / f"iter_{k:03d}_train.csv")
        traces = {t.id: format_trace(t, results[t.id], outcomes[t.id]) for t in tasks}
        self.ws.write_raw(k, traces, {tid: o.__dict__ for tid, o in outcomes.items()})
        log.info("  train score %.4f", mean(outcomes))
        return outcomes

    def _maintain(self, k: int, outcomes: Dict[str, Outcome]) -> None:
        backup = self._iter_dir(k) / "wiki_before_maintainer"
        if backup.is_dir():  # an earlier attempt stopped mid-way: start from the same wiki
            shutil.rmtree(self.ws.wiki)
            shutil.copytree(backup, self.ws.wiki)
        else:
            _copy_atomic(self.ws.wiki, backup)
        sampled = sample_traces(outcomes, seed=self.opts.seed * 1000 + k)
        traces_dir = self.ws.raw_dir(k) / "traces"
        traces = {tid: read_text(traces_dir / f"{tid}.md") for tid in sampled}
        warnings = run_maintainer(self.backend, self.ws, k, traces, load_skills(self.ws.skills))
        log.info("  wiki updated from %d traces (%d skipped edits)", len(traces), len(warnings))

    def _validate(self, k: int, r_best: float) -> Dict[str, Any]:
        """Build S'_k = Apply(S_{k-1}, P_k) and score it on the validation split."""
        proposal = read_json(self._iter_dir(k) / "proposal.json")
        pending = {"proposal": proposal, "target": proposal.get("name"), "diff": "", "score": None,
                   "r_best_before": r_best, "accepted": False}
        if proposal.get("action") == "no_action":
            return pending
        candidate = self._iter_dir(k) / "candidate_skills"
        if not candidate.is_dir():
            staging = self._iter_dir(k) / ".candidate_staging"
            apply_proposal(self.ws.skills, proposal, staging)
            staging.rename(candidate)
        pending["diff"] = apply_proposal(self.ws.skills, proposal, self._iter_dir(k) / ".diff_check")[1]
        shutil.rmtree(self._iter_dir(k) / ".diff_check")
        outcomes = self._score_split("val", load_skills(candidate), f"iter_{k:03d}_val",
                                     self._iter_dir(k) / "val")
        pending["score"] = mean(outcomes)
        pending["accepted"] = pending["score"] > r_best  # strict improvement (Eq. 4)
        return pending

    def _commit(self, state: Dict[str, Any]) -> None:
        """Gate: accept or roll back skills; the wiki is kept either way."""
        k, pending = state["iteration"], state["pending"]
        marker = self._iter_dir(k) / "skills_swapped"
        if pending["accepted"] and not marker.exists():
            shutil.rmtree(self.ws.skills)
            shutil.copytree(self._iter_dir(k) / "candidate_skills", self.ws.skills)
            _note_purpose(self.ws.skills / pending["target"], k, pending)
            marker.write_text("done\n", encoding="utf-8")
        _record_skill_impact(self.ws, k, pending)
        if pending["accepted"]:
            state["r_best"] = pending["score"]
        verdict = "accepted" if pending["accepted"] else ("no_action" if pending["score"] is None else "rejected")
        log.info("  proposal %s (%s %s), val score %s, best %.4f", verdict, pending["proposal"].get("action"),
                 pending["target"] or "", "-" if pending["score"] is None else f"{pending['score']:.4f}",
                 state["r_best"])
        state["history"].append({"iteration": k, "action": pending["proposal"].get("action"),
                                 "target": pending["target"], "val_score": pending["score"],
                                 "r_best": state["r_best"], "outcome": verdict})
        state.update(iteration=k + 1, stage="rollout", pending=None)
        self.ws.save_state(state)

    def _score_split(self, split: str, skills: List[Tuple[str, str]], label: str,
                     stage_dir: Path) -> Dict[str, Outcome]:
        tasks = self.suite.split(split)
        results = run_tasks(self.backend, self.suite, tasks, skills, stage_dir, self.opts.workers)
        return self.scorer.score_all(tasks, results, self.ws.reviews / f"{label}.csv")


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy a folder so that `dst` either does not exist or is complete."""
    staging = dst.with_name(dst.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging)
    staging.rename(dst)


def task_fingerprint(suite: Suite) -> Dict[str, List[str]]:
    return {split: sorted(t.id for t in suite.split(split)) for split in ("train", "val")}


def _note_purpose(skill_dir: Path, k: int, pending: Dict[str, Any]) -> None:
    """Keep PURPOSE.md's Evolution History in step with accepted patches."""
    if pending["proposal"].get("action") == "patch":
        append_text(skill_dir / "PURPOSE.md",
                    f"\n- Iteration {k}: patch accepted (validation {pending['score']:.4f}).\n")


def _record_skill_impact(ws: Workspace, k: int, pending: Dict[str, Any]) -> None:
    """Programmatic audit trail W_k <- Update(W'_k, P_k, R(T_val,k), a_k) (paper §3.2.4)."""
    header = f"## Iteration {k} "
    path = ws.wiki / "skill-impact.md"
    if header in read_text(path):
        return  # already written by an interrupted earlier attempt
    proposal = pending["proposal"]
    action = proposal.get("action")
    if pending["score"] is None:
        body = f"{header}- NO_ACTION\n\n- Reason: {proposal.get('reason', 'proposer chose no change')}\n"
    else:
        outcome = "ACCEPTED" if pending["accepted"] else "REJECTED"
        body = (
            f"{header}- {outcome}\n\n- Action: {action} skill `{pending['target']}`\n"
            f"- Validation score: {pending['score']:.4f} (best before: {pending['r_best_before']:.4f})\n"
            f"\n```diff\n{pending['diff']}```\n"
        )
        if isinstance(proposal.get("purpose_md"), str):
            body += f"\nPURPOSE.md of the proposal:\n\n```markdown\n{proposal['purpose_md'].rstrip()}\n```\n"
    append_text(path, "\n" + body)
