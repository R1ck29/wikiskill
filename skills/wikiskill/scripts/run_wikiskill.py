"""Run the wikiskill CLI from a skill folder: `python run_wikiskill.py <command> ...`.

The `wikiskill` package is looked up first inside the skill folder (a copy made by
tools/integrate.py) and then in each parent folder (the repository / plugin root).
If this Python is older than 3.9, the newest `python3.X` on PATH runs the command instead.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def find_package_root(start: Path) -> Optional[Path]:
    """Return the folder that contains the `wikiskill` package, searching `start` and its parents."""
    for folder in (start, *start.parents):
        if (folder / "wikiskill" / "__init__.py").is_file():
            return folder
    return None


def newer_python() -> Optional[str]:
    """Path of the newest `python3.X` (X >= 9) on PATH, or None."""
    for minor in range(30, 8, -1):
        exe = shutil.which(f"python3.{minor}")
        if exe:
            return exe
    return None


def main() -> int:
    if sys.version_info < (3, 9) and not os.environ.get("WIKISKILL_REEXEC"):
        exe = newer_python()
        if exe:
            env = dict(os.environ, WIKISKILL_REEXEC="1")
            command = [exe, str(Path(__file__).resolve()), *sys.argv[1:]]
            return subprocess.run(command, env=env, check=False).returncode
    skill_dir = Path(__file__).resolve().parent.parent
    root = find_package_root(skill_dir)
    if root is None:
        print(f"error: the wikiskill package was not found in {skill_dir} or its parent folders", file=sys.stderr)
        return 2
    sys.path.insert(0, str(root))
    from wikiskill.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
