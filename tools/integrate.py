"""Copy WikiSkill into an existing Claude Code plugin, project or skills folder as one self-contained skill.

    python tools/integrate.py <plugin folder>                 -> <plugin>/skills/wikiskill/
    python tools/integrate.py <project folder>                -> <project>/.claude/skills/wikiskill/
    python tools/integrate.py <skills folder> --skills-dir    -> <skills folder>/wikiskill/
    add --force to replace a WikiSkill copy made earlier (for updates)

A plugin folder has .claude-plugin/plugin.json; a project folder has a .claude folder.

The copy holds SKILL.md, the launcher, the `wikiskill` package, docs/ and examples/, so it works
without this repository (unzip the repository ZIP anywhere, run this once, then delete the ZIP folder).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_NAME = "wikiskill"
LAUNCHER = Path("scripts") / "run_wikiskill.py"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def destination(target: Path, skills_dir: bool) -> Path:
    if skills_dir:
        return target / SKILL_NAME
    if (target / ".claude-plugin" / "plugin.json").is_file():
        return target / "skills" / SKILL_NAME
    if (target / ".claude").is_dir():
        return target / ".claude" / "skills" / SKILL_NAME
    raise ValueError(f"{target} is neither a plugin folder (.claude-plugin/plugin.json) nor a project folder "
                     "(.claude/); to copy into a folder of skills, pass that folder with --skills-dir")


def integrate(target: Path, skills_dir: bool = False, force: bool = False) -> Path:
    """Copy the self-contained skill folder and return its path."""
    dest = destination(target.resolve(), skills_dir)
    if dest.is_symlink():
        raise FileExistsError(f"{dest} is a symbolic link; remove it first")
    dest = dest.resolve()  # a linked parent folder must not lead back into this repository
    if dest == REPO or REPO in dest.parents:  # copying into the source would copy itself again and again
        raise ValueError(f"{target} leads inside this repository; choose a folder outside {REPO}")
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists; pass --force to replace it")
        if not (dest / LAUNCHER).is_file():  # never delete a folder this tool did not create
            raise FileExistsError(f"{dest} exists but is not a WikiSkill copy; remove or rename it first")
    new, old = dest.with_name(f".{SKILL_NAME}.new"), dest.with_name(f".{SKILL_NAME}.old")
    for leftover in (new, old):  # from an earlier run that was cut short
        _remove(leftover)
    try:  # build the whole copy first: a failed copy never costs the working one
        shutil.copytree(REPO / "skills" / SKILL_NAME, new, ignore=IGNORE)
        for name in ("wikiskill", "docs", "examples"):
            shutil.copytree(REPO / name, new / name, ignore=IGNORE)
    except BaseException:
        _remove(new)
        raise
    if dest.exists():
        dest.rename(old)
    try:
        new.rename(dest)
    except OSError:  # e.g. a file held open on Windows: put the working copy back
        if old.exists():
            old.rename(dest)
        _remove(new)
        raise
    with contextlib.suppress(OSError):  # a running improve may still hold old files; the next run removes them
        _remove(old)
    return dest


def _remove(path: Path) -> None:
    """Delete a folder, including files a ZIP extract left read-only (Windows refuses to delete those)."""
    def make_writable(func, failed, _exc):
        os.chmod(failed, stat.S_IWRITE)
        func(failed)
    if path.exists():  # `onerror` is deprecated from Python 3.12 on, `onexc` does not exist before it
        shutil.rmtree(path, **{"onexc" if sys.version_info >= (3, 12) else "onerror": make_writable})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=lambda s: Path(s).expanduser(), help="plugin or project folder (or skills folder with --skills-dir); ~ is expanded")
    parser.add_argument("--skills-dir", action="store_true", help="target is a skills folder, not a plugin")
    parser.add_argument("--force", action="store_true", help="replace a WikiSkill copy made earlier")
    args = parser.parse_args(argv)
    try:
        dest = integrate(args.target, args.skills_dir, args.force)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # includes shutil.Error: disk full, permissions, locked files
        print(f"error: copy failed: {e}", file=sys.stderr)
        return 1
    launcher = dest / LAUNCHER
    print(f"copied WikiSkill to {dest}\n"
          f"check: python \"{launcher}\" check --no-call   (python3 on macOS/Linux, py on Windows)\n"
          "then start a new Claude session; the skill is /wikiskill (in a plugin: /<plugin name>:wikiskill)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
