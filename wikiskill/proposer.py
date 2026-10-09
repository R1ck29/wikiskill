"""Wiki-informed Skill Proposer: a ReAct agent with read_file and finish (paper §3.2.3).

It starts from the wiki index, skill-impact.md and a summary of all training outcomes,
reads pattern pages and traces on demand, and finishes with one atomic proposal:
create a skill, patch one skill, or no_action.

The loop is implemented here (one `complete` call per turn), so it behaves the same
with every backend and the agent can only read inside the workspace.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .config import Suite, Task
from .scoring import Outcome
from .skills import SkillError, apply_proposal, load_skills
from .util import extract_json_object, read_text, truncate, write_text
from .workspace import Workspace

log = logging.getLogger(__name__)

PROMPT = (Path(__file__).parent / "prompts" / "skill_proposer.md").read_text(encoding="utf-8")
OBSERVATION_CHARS = 15000
CELL_CHARS = 200


class ReadSandbox:
    """read_file for the proposer: only wiki/, skills/ and traces/<task_id> (aliased to raw/)."""

    def __init__(self, ws: Workspace, iteration: int) -> None:
        self.ws, self.traces_dir = ws, ws.raw_dir(iteration) / "traces"
        self.traces_read: Set[str] = set()

    def read(self, path: str) -> str:
        try:
            return self._read(path)
        except (OSError, ValueError) as e:  # e.g. NUL bytes or names the OS rejects
            return f"ERROR: cannot read {str(path)[:200]!r} ({type(e).__name__})."

    def _read(self, path: str) -> str:
        path = str(path).strip().replace("\\", "/").lstrip("/")
        if path.startswith("traces/"):
            task_id = path[len("traces/"):].rstrip("/")
            target = self.traces_dir / (task_id if task_id.endswith(".md") else f"{task_id}.md")
            base = self.traces_dir
        elif path.split("/")[0] in ("wiki", "skills"):
            target, base = self.ws.root / path, self.ws.root / path.split("/")[0]
        else:
            return "ERROR: only wiki/..., skills/... and traces/<task_id> can be read."
        target = target.resolve()
        if target != base.resolve() and base.resolve() not in target.parents:
            return "ERROR: path escapes the allowed folders."
        if target.is_dir():
            names = sorted(p.name for p in target.iterdir())
            return "Directory listing:\n" + ("\n".join(names) if names else "(empty)")
        if not target.is_file():
            return f"ERROR: {path} does not exist."
        if target.parent == self.traces_dir.resolve():
            self.traces_read.add(target.stem.lower())  # macOS/Windows paths ignore case
        return truncate(read_text(target), OBSERVATION_CHARS)


def outcome_summary(tasks: List[Task], outcomes: Dict[str, Outcome]) -> str:
    """Concise table of every training task: pass/fail, prediction and ground truth."""
    rows = ["| task_id | result | score | agent answer | expected answer / reviewer note |", "|---|---|---|---|---|"]
    for task in tasks:
        o = outcomes[task.id]
        expected = str(task.answer) if task.answer is not None else (o.note or "-")
        rows.append(f"| {task.id} | {'PASS' if o.passed else 'FAIL'} | {o.score:g} | "
                    f"{_cell(o.prediction)} | {_cell(expected)} |")
    return "\n".join(rows)


def _cell(text: str) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS] + "..."


def initial_context(ws: Workspace, tasks: List[Task], outcomes: Dict[str, Outcome]) -> str:
    skills = [name for name, _ in load_skills(ws.skills)]
    return (
        f"## wiki/index.md\n{read_text(ws.wiki / 'index.md')}\n\n"
        f"## wiki/skill-impact.md\n{truncate(read_text(ws.wiki / 'skill-impact.md'), OBSERVATION_CHARS)}\n\n"
        f"## Active skills\n{', '.join(skills) or '(none yet)'}\n\n"
        f"## Training task outcomes (latest iteration)\n{outcome_summary(tasks, outcomes)}\n"
    )


def run_proposer(backend: Any, ws: Workspace, suite: Suite, iteration: int, tasks: List[Task],
                 outcomes: Dict[str, Outcome], max_turns: int = 20, min_traces: int = 4) -> Dict[str, Any]:
    """Run the ReAct loop and return a validated proposal (create / patch / no_action)."""
    system = PROMPT.replace("{task_desc}", suite.task_description)
    sandbox = ReadSandbox(ws, iteration)
    required_reads = min(min_traces, len(tasks))
    transcript = [f"# Initial context\n\n{initial_context(ws, tasks, outcomes)}"]
    log_path = ws.runs / f"iter_{iteration:03d}" / "proposer_transcript.md"
    for turn in range(1, max_turns + 1):
        if turn == max_turns:
            transcript.append("NOTE: this is your last turn. You must call finish now.")
        reply = backend.complete("\n\n".join(transcript) + "\n\nYour next reply (one JSON object):", system=system)
        observation, proposal = _step(reply, sandbox, ws, required_reads)
        transcript.append(f"## Turn {turn}\nYou: {reply.strip()}\n\nTool result:\n{observation}")
        write_text(log_path, "\n\n".join(transcript) + "\n")
        if proposal is not None:
            log.info("  proposer finished after %d turns: %s %s", turn, proposal.get("action"), proposal.get("name", ""))
            return proposal
    log.warning("  proposer hit the turn limit (%d) without a valid proposal", max_turns)
    return {"action": "no_action", "reason": f"no valid proposal within {max_turns} turns"}


def _step(reply: str, sandbox: ReadSandbox, ws: Workspace, required_reads: int):
    """Execute one tool call. Returns (observation, proposal or None)."""
    try:
        action = extract_json_object(reply)
    except ValueError:
        return "ERROR: reply with exactly one JSON object as described.", None
    tool = action.get("tool")
    if tool == "read_file":
        return sandbox.read(str(action.get("path", ""))), None
    if tool != "finish":
        return "ERROR: 'tool' must be 'read_file' or 'finish'.", None
    proposal = action.get("proposal")
    error = validate_proposal(proposal, ws, len(sandbox.traces_read), required_reads)
    return (f"ERROR: {error}", None) if error else ("Proposal accepted for validation.", proposal)


def validate_proposal(proposal: Any, ws: Workspace, traces_read: int, required_reads: int) -> Optional[str]:
    """Return an error message, or None if the proposal can be applied."""
    if not isinstance(proposal, dict):
        return "'proposal' must be a JSON object."
    if proposal.get("action") == "no_action":
        return None
    if traces_read < required_reads:
        return f"read at least {required_reads} execution traces before proposing (read so far: {traces_read})."
    with tempfile.TemporaryDirectory(prefix="wikiskill-dryrun-") as tmp:
        try:
            apply_proposal(ws.skills, proposal, Path(tmp) / "skills")
        except SkillError as e:
            return f"invalid proposal: {e}"
    return None
