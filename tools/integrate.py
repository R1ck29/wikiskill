"""Copy WikiSkill into an existing Claude Code plugin as one self-contained skill folder.

    python tools/integrate.py <plugin folder>                 -> <plugin>/skills/wikiskill/
    python tools/integrate.py <skills folder> --skills-dir    -> <skills folder>/wikiskill/
    add --force to replace a WikiSkill copy made earlier (for updates)

The copy holds SKILL.md, the launcher, the `wikiskill` package, docs/ and examples/, so it works
without this repository (unzip the repository ZIP anywhere, run this once, then delete the ZIP folder).
"""
import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional

REPO = Path(__file__).resolve().parent.parent
SKILL_NAME = "wikiskill"
LAUNCHER = Path("scripts") / "run_wikiskill.py"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def destination(target: Path, skills_dir: bool) -> Path:
    if skills_dir:
        return target / SKILL_NAME
    if not (target / ".claude-plugin" / "plugin.json").is_file():
        raise ValueError(f"{target} is not a plugin folder (no .claude-plugin/plugin.json); "
                         "pass --skills-dir to copy into a plain skills folder")
    return target / "skills" / SKILL_NAME


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
        shutil.rmtree(dest)
    shutil.copytree(REPO / "skills" / SKILL_NAME, dest, ignore=IGNORE)
    for name in ("wikiskill", "docs", "examples"):
        shutil.copytree(REPO / name, dest / name, ignore=IGNORE)
    return dest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="plugin folder (or skills folder with --skills-dir)")
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
    if args.skills_dir:
        print(f"copied WikiSkill to {dest}\nstart a new Claude session to use it (/wikiskill)")
    else:
        print(f"copied WikiSkill to {dest}\n"
              f"check: claude plugin validate \"{args.target}\"   (then restart Claude)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
