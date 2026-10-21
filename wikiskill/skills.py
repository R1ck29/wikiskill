"""Skill Layer: SKILL.md validation, prompt rendering and proposal application.

Each skill is a directory `skills/<name>/` holding SKILL.md (injected into the
Inference Agent) and PURPOSE.md (links the skill to the wiki patterns behind it).
Names follow the Agent Skills spec (https://agentskills.io/specification) so the
evolved skills can be copied straight into Claude Code or Codex.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from .patching import PatchError, apply_edits, unified_diff
from .util import is_windows_reserved, read_text, write_text

NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
MAX_NAME, MAX_DESCRIPTION = 64, 1024
# ends every exported SKILL.md, so a later export knows wikiskill wrote it ("feedback": kept from 0.2.0 exports)
EXPORT_MARK = "<!-- wikiskill:feedback -->"


class SkillError(ValueError):
    """A skill or a proposal violates the format."""


def parse_frontmatter(text: str) -> dict:
    """Parse the simple `key: value` YAML frontmatter used by SKILL.md (supports > and | blocks)."""
    lines = text.lstrip("\ufeff").splitlines()  # tolerate a BOM from Windows editors
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with '---' YAML frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillError("SKILL.md frontmatter is not closed with '---'") from None
    fields, key = {}, None
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            fields[key] = "" if value in (">", "|", ">-", "|-") else _unquote(value)
        elif key and line.startswith((" ", "\t")):
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def validate_name(name: object) -> None:
    if (not isinstance(name, str) or len(name) > MAX_NAME or not NAME_RE.fullmatch(name)
            or is_windows_reserved(name)):
        raise SkillError(
            f"invalid skill name {name!r}: use 1-{MAX_NAME} lowercase letters, digits and single hyphens"
        )


def validate_skill_md(name: str, text: str) -> None:
    validate_name(name)
    meta = parse_frontmatter(text)
    if meta.get("name") != name:
        raise SkillError(f"frontmatter name {meta.get('name')!r} must equal the directory name {name!r}")
    description = meta.get("description", "")
    if not 1 <= len(description) <= MAX_DESCRIPTION:
        raise SkillError(f"frontmatter description must be 1-{MAX_DESCRIPTION} characters")


def load_skills(skills_dir: Path) -> list[tuple[str, str]]:
    """Return [(name, SKILL.md text)] sorted by name."""
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        return []
    return [
        (d.name, read_text(d / "SKILL.md"))
        for d in sorted(skills_dir.iterdir())
        if d.is_dir() and (d / "SKILL.md").is_file()
    ]


def copy_skills(skills_dir: Path, dest: Path, may_overwrite: Callable[[Path], bool],
                footer: str = "") -> list[tuple[str, Path, bool]]:
    """Copy each SKILL.md to dest/<name>/SKILL.md (plus EXPORT_MARK and `footer`); return (name, target, written)."""
    copied = []
    for name, text in load_skills(skills_dir):
        target = Path(dest) / name / "SKILL.md"
        if target.exists() and not may_overwrite(target):
            copied.append((name, target, False))
            continue
        write_text(target, text.rstrip("\n") + f"\n\n{EXPORT_MARK}\n{footer}")
        copied.append((name, target, True))
    return copied


def exported_by_wikiskill(target: Path) -> bool:
    """True when `target` was written by copy_skills, so a later export may replace it."""
    return EXPORT_MARK in read_text(target)


def render_skill_section(skills: list[tuple[str, str]]) -> str:
    """Full injection of every active skill into the system prompt (paper §3.2.1)."""
    if not skills:
        return ""
    blocks = "\n\n".join(f'<skill name="{name}">\n{text.strip()}\n</skill>' for name, text in skills)
    return (
        "## Skills\n"
        "The following skills contain procedural guidance learned from earlier tasks. "
        "Apply each skill whenever its conditions match.\n\n" + blocks + "\n"
    )


def apply_proposal(src: Path, proposal: dict, dst: Path) -> tuple[str, str]:
    """Copy skill set `src` to `dst` and apply a create/patch proposal there.

    Returns (target skill name, unified diff). Raises SkillError if the proposal is invalid;
    `src` is never modified, so rejecting a candidate is just deleting `dst`.
    """
    if not isinstance(proposal, dict):
        raise SkillError("proposal must be a JSON object")
    action, name = proposal.get("action"), proposal.get("name")
    if action not in ("create", "patch"):
        raise SkillError("proposal 'action' must be 'create', 'patch' or 'no_action'")
    validate_name(name)
    if dst.exists():
        shutil.rmtree(dst)
    if Path(src).is_dir():
        shutil.copytree(src, dst)
    else:
        dst.mkdir(parents=True)
    skill_dir = dst / name
    old = read_text(skill_dir / "SKILL.md") if (skill_dir / "SKILL.md").is_file() else None
    new = _create(skill_dir, name, proposal) if action == "create" else _patch(skill_dir, name, proposal, old)
    return name, unified_diff(old or "", new, f"{name}/SKILL.md", is_new=old is None)


def _create(skill_dir: Path, name: str, proposal: dict) -> str:
    skill_md, purpose_md = proposal.get("skill_md"), proposal.get("purpose_md")
    if not isinstance(skill_md, str) or not isinstance(purpose_md, str):
        raise SkillError("a 'create' proposal needs string 'skill_md' and 'purpose_md'")
    validate_skill_md(name, skill_md)
    write_text(skill_dir / "SKILL.md", skill_md)
    write_text(skill_dir / "PURPOSE.md", purpose_md)
    return skill_md


def _patch(skill_dir: Path, name: str, proposal: dict, old: str | None) -> str:
    if old is None:
        raise SkillError(f"cannot patch unknown skill {name!r}; use action 'create'")
    try:
        new = apply_edits(old, proposal.get("edits"))
    except PatchError as e:
        raise SkillError(f"patch failed: {e}") from e
    validate_skill_md(name, new)
    write_text(skill_dir / "SKILL.md", new)
    return new
