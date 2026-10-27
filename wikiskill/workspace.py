"""The WikiSkill workspace: three knowledge layers plus run bookkeeping (paper §3.1).

    raw/      Raw Layer   - immutable training traces, one folder per iteration (write once)
    wiki/     Wiki Layer  - index.md, logs.md, skill-impact.md, patterns/*.md (never reset)
    skills/   Skill Layer - active skills; replaced only when a candidate passes gating
    runs/     rollout cache, proposals and candidates (lets an interrupted run resume)
    reviews/  CSV sheets for human review
    state.json  loop progress;  run.log  log of evolve/improve;  evolve.lock  present while a run holds it
"""
from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Refuse a second `evolve` on the same workspace while one is running."""
        path = self.root / "evolve.lock"
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(f"{path} exists: another evolve is running on this workspace. "
                               "If none is (e.g. the computer crashed), delete the file and retry.") from None
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    # ----- state -------------------------------------------------------------
    def load_state(self) -> dict[str, Any] | None:
        return read_json(self.state_path) if self.state_path.is_file() else None

    def save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)

    # ----- raw layer -----------------------------------------------------------
    def raw_dir(self, iteration: int) -> Path:
        return self.raw / f"iter_{iteration:03d}"

    def write_raw(self, iteration: int, traces: dict[str, str], summary: Any) -> Path:
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

    def iter_dir(self, iteration: int) -> Path:
        """Run cache of one iteration: train/val rollouts, proposal, candidate skills."""
        return self.stage_dir(f"iter_{iteration:03d}")


def resolve_workspace(root: Path) -> Workspace:
    """The workspace at `root`, or its newest gen-NNN folder (layout written by improve in 0.2.x)."""
    gens = sorted(p for p in Path(root).glob("gen-[0-9][0-9][0-9]") if p.is_dir())
    return Workspace(gens[-1] if gens else root)
