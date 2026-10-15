"""Test helpers: a scripted in-process backend for loop tests (no CLI, no network), temp-folder tests."""
import json
import tempfile
import unittest
from pathlib import Path

from wikiskill.backends import AgentResult

GOOD_SKILL = ("---\nname: report-total\ndescription: Report the grand total. Use for every sales question.\n---\n"
              "# Instructions\nAlways answer with the TOTAL row.\n")
BAD_SKILL = "---\nname: be-careful\ndescription: Be careful. Use always.\n---\nThink harder.\n"


class FakeBackend:
    """Inference agent answers correctly only when the `report-total` skill is injected.

    The Skill Proposer first proposes an unhelpful skill, then (after seeing the rejection in
    skill-impact.md) the helpful one. The Wiki Maintainer creates one pattern page.
    """

    name = "fake"

    def __init__(self, crash_proposer_once=False):
        self.crash_proposer_once = crash_proposer_once
        self.agent_calls, self.complete_calls = 0, []

    def run_agent(self, prompt, system, cwd):
        self.agent_calls += 1
        answer = "TOTAL" if system and '<skill name="report-total">' in system else "partial"
        (Path(cwd) / "note.txt").write_text(answer, encoding="utf-8")
        return AgentResult(f"<answer>{answer}</answer>", f"ran in {Path(cwd).name}", ok=True)

    def complete(self, prompt, system=None):
        self.complete_calls.append((system or "")[:40])
        if system and system.startswith("You are a Wiki Maintainer"):
            return json.dumps({
                "create_patterns": [{"name": "partial-answers.md", "content": "# Partial answers\nEvidence."}],
                "update_index": "# Wiki Index\n- [partial-answers](wiki/patterns/partial-answers.md): agents "
                                "report partial sums; root cause: no total rule; fix: report TOTAL.\n",
                "append_log": "Found partial answers.",
            })
        if system and system.startswith("You are a Skill Proposer"):
            return self._proposer_turn(prompt)
        return '{"score": 1, "reason": "judge"}'

    def _proposer_turn(self, prompt):
        if self.crash_proposer_once:
            self.crash_proposer_once = False
            raise RuntimeError("simulated crash")
        reads = prompt.count('"tool": "read_file"')
        paths = ["wiki/index.md", "traces/t1", "traces/t2", "traces/t3", "traces/t4"]
        if reads < len(paths):
            return json.dumps({"thought": "look", "tool": "read_file", "path": paths[reads]})
        if "be-careful" in prompt.split("# Initial context", 1)[1].split("## Turn 1", 1)[0]:
            skill = {"action": "create", "name": "report-total", "skill_md": GOOD_SKILL,
                     "purpose_md": "# Purpose\nPatterns: partial-answers.md\n"}
        else:
            skill = {"action": "create", "name": "be-careful", "skill_md": BAD_SKILL, "purpose_md": "# Purpose\n"}
        return json.dumps({"thought": "propose", "tool": "finish", "proposal": skill})


class TempDirTest(unittest.TestCase):
    """A test case with a fresh temporary folder in self.root."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
