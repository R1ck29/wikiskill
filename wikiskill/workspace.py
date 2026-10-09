"""The WikiSkill workspace: three knowledge layers plus run bookkeeping (paper §3.1).

    raw/      Raw Layer   - immutable training traces, one folder per iteration (write once)
    wiki/     Wiki Layer  - index.md, logs.md, skill-impact.md, patterns/*.md (never reset)
    skills/   Skill Layer - active skills; replaced only when a candidate passes gating
    runs/     rollout cache, proposals and candidates (lets an interrupted run resume)
    reviews/  CSV sheets for human review
    state.json  loop progress
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .util import read_json, write_json, write_text

WIKI_SEED = {
    "index.md": "# Wiki Index\n\nOne line per pattern: PROBLEM + ROOT CAUSE + FIX.\n\n",
    "logs.md": "# Evolution Log\n\nChronological findings, appended by the Wiki Maintainer.\n\n",
    "skill-impact.md": (
        "# Skill Impact\n\nEvery skill proposal with its diff, validation score and outcome. "
        "Written by the harness after validation gating.\n\n"
    ),
}


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.raw = self.root / "raw"
        self.wiki = self.root / "wiki"
        self.skills = self.root / "skills"
        self.runs = self.root / "runs"
        self.reviews = self.root / "reviews"
        self.state_path = self.root / "state.json"

    def init(self) -> None:
        for d in (self.raw, self.wiki / "patterns", self.skills, self.runs, self.reviews):
            d.mkdir(parents=True, exist_ok=True)
        for name, text in WIKI_SEED.items():
            if not (self.wiki / name).exists():
                write_text(self.wiki / name, text)

    # ----- state -------------------------------------------------------------
    def load_state(self) -> Optional[Dict[str, Any]]:
        return read_json(self.state_path) if self.state_path.is_file() else None

    def save_state(self, state: Dict[str, Any]) -> None:
        write_json(self.state_path, state)

    # ----- raw layer -----------------------------------------------------------
    def raw_dir(self, iteration: int) -> Path:
        return self.raw / f"iter_{iteration:03d}"

    def write_raw(self, iteration: int, traces: Dict[str, str], summary: Any) -> Path:
        """Store one iteration's training traces. The Raw Layer is write-once."""
        final = self.raw_dir(iteration)
        if final.exists():
            raise FileExistsError(f"raw layer is immutable; {final} already exists")
        if len({task_id.lower() for task_id in traces}) != len(traces):
            raise ValueError("task ids collide on a case-insensitive file system")
        staging = self.raw / f".staging_iter_{iteration:03d}"
        if staging.exists():
            shutil.rmtree(staging)
        for task_id, text in traces.items():
            write_text(staging / "traces" / f"{task_id}.md", text)
        write_json(staging / "summary.json", summary)
        staging.rename(final)
        return final

    # ----- run cache -------------------------------------------------------------
    def stage_dir(self, stage: str) -> Path:
        return self.runs / stage
