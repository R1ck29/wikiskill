"""Copy WikiSkill into an existing Claude Code plugin, project or skills folder as one self-contained skill.

    python tools/integrate.py <plugin folder>                 -> <plugin>/skills/wikiskill/
    python tools/integrate.py <project folder>                -> <project>/.claude/skills/wikiskill/
    python tools/integrate.py <skills folder> --skills-dir    -> <skills folder>/wikiskill/
    add --force to replace a WikiSkill copy made earlier (for updates)
    add --no-hook to leave the reminder hook out (and remove one added earlier)

A plugin folder has .claude-plugin/plugin.json; a project folder has a .claude folder.

It also registers a UserPromptSubmit hook (scripts/judgement_hook.py) that reminds Claude to record
a prompt that looks like a judgement of the last result: in ~/.claude/settings.json for the global
skills folder, <project>/.claude/settings.local.json for a project (personal, not committed) and
<plugin>/hooks/hooks.json for a plugin. Other settings and hooks in those files are kept.

The copy holds SKILL.md, the launcher, the `wikiskill` package, docs/ and examples/, so it works
without this repository (unzip the repository ZIP anywhere, run this once, then delete the ZIP folder).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_NAME = "wikiskill"
LAUNCHER = Path("scripts") / "run_wikiskill.py"
HOOK = Path("scripts") / "judgement_hook.py"
HOOK_EVENT = "UserPromptSubmit"
MANAGED = (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),  # macOS
           Path("/etc/claude-code/managed-settings.json"),  # Linux
           Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json")
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


def hook_target(target: Path, skills_dir: bool, dest: Path) -> tuple[Path, str] | None:
    """(settings file, hook command) for this kind of target, or None when there is no settings file to use."""
    target = target.resolve()
    command = _command(sys.executable, str(dest / HOOK))
    if skills_dir:
        if target.name != "skills" or target.parent.name != ".claude":
            return None
        if target.parent == (Path.home() / ".claude").resolve():
            return target.parent / "settings.json", command
        return target.parent / "settings.local.json", command  # a project's: keep machine paths out of the shared file
    if (target / ".claude-plugin" / "plugin.json").is_file():  # the plugin may move: Claude Code fills in its folder
        python = "py" if os.name == "nt" else "python3"
        return target / "hooks" / "hooks.json", f'{python} "${{CLAUDE_PLUGIN_ROOT}}/skills/{SKILL_NAME}/{HOOK.as_posix()}"'
    return target / ".claude" / "settings.local.json", command


def _command(python: str, script: str) -> str:
    """A shell command line for the hook. Claude Code runs hooks with sh/bash (Git Bash on Windows)."""
    if os.name == "nt":  # Windows paths cannot contain '"', so double quotes are enough for bash and cmd
        return f'"{python}" "{script}"'
    return f"{shlex.quote(python)} {shlex.quote(script)}"


def set_hook(settings: Path, command: str | None) -> None:
    """Replace WikiSkill's hook in `settings` with `command` (None: only remove it), keeping everything else."""
    original = json.loads(settings.read_text(encoding="utf-8")) if settings.is_file() else {}
    data = json.loads(json.dumps(original))  # a deep copy, compared below
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise TypeError(f"{settings} does not have the usual form; add the hook by hand (docs/CONFIGURATION.md)")
    groups = data.setdefault("hooks", {}).get(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise TypeError(f"{settings}: hooks.{HOOK_EVENT} is not a list; add the hook by hand (docs/CONFIGURATION.md)")
    kept = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            others = [h for h in group["hooks"] if not _ours(h)]
            if others:
                kept.append({**group, "hooks": others})
        else:
            kept.append(group)
    if command:
        kept.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    if kept:
        data["hooks"][HOOK_EVENT] = kept
    else:
        data["hooks"].pop(HOOK_EVENT, None)
        if not data["hooks"] and "hooks" not in original:
            data.pop("hooks")
    if data == original or (not settings.is_file() and not data):
        return  # nothing to change: leave the file (and its formatting) alone
    real = settings.resolve() if settings.is_symlink() else settings  # keep a dotfiles link a link
    real.parent.mkdir(parents=True, exist_ok=True)
    backup = settings.with_name(settings.name + ".wikiskill.bak")
    if real.is_file() and not backup.exists():
        shutil.copy2(str(real), str(backup))
    tmp = real.with_name(f".{real.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if real.is_file():
        shutil.copymode(str(real), str(tmp))
    os.replace(str(tmp), str(real))


def _ours(hook: object) -> bool:
    command = hook.get("command", "") if isinstance(hook, dict) else ""
    return isinstance(command, str) and HOOK.name in command and SKILL_NAME in command


def hooks_blocked(settings: Path) -> str | None:
    """Why Claude Code would not run the hook (managed policy or disableAllHooks), or None."""
    for path, key in [(p, "allowManagedHooksOnly") for p in MANAGED] + [(settings, "disableAllHooks")]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (data.get(key) is True or data.get("disableAllHooks") is True):
            return f"{path} turns hooks off"
    return None


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
    parser.add_argument("--no-hook", action="store_true", help="do not add the reminder hook; remove one added earlier")
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
    print(f"copied WikiSkill to {dest}")
    code = _hook(args.target, args.skills_dir, dest, not args.no_hook)
    print(f"check: python \"{launcher}\" check --no-call   (python3 on macOS/Linux, py on Windows)\n"
          "then start a new Claude session; the skill is /wikiskill (in a plugin: /<plugin name>:wikiskill)")
    return code


def _hook(target: Path, skills_dir: bool, dest: Path, add: bool) -> int:
    """Add (or remove) the reminder hook and say what happened; 1 when it could not be added."""
    found = hook_target(target, skills_dir, dest)
    if found is None:
        if add:
            print("hook: not added (this skills folder has no settings file next to it); "
                  "to add it by hand see docs/CONFIGURATION.md")
        return 0
    settings, command = found
    try:
        set_hook(settings, command if add else None)
    except (OSError, ValueError, TypeError) as e:
        print(f"warning: the skill is installed, but the hook was not changed: {e}", file=sys.stderr)
        return 1
    if not add:
        print(f"hook: removed from {settings} (if it was there)")
        return 0
    print(f"hook: {HOOK_EVENT} added to {settings}")
    blocked = hooks_blocked(settings)
    if blocked:
        print(f"warning: {blocked}; judgements are then noticed only through the skill's description")
    return 0


if __name__ == "__main__":
    sys.exit(main())
