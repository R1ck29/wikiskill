"""Command line interface: `python -m wikiskill <command> ...` (see README.md)."""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import platform
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import __version__
from .backends import make_backend
from .config import ConfigError, load_suite
from .evaluate import compare_reports, evaluate
from .evolve import Evolution, EvolveOptions, has_work
from .improve import (
    MIN_NEW,
    export_skills,
    feedback_footer,
    merge_ready,
    pending,
    record_feedback,
)
from .review import apply_sheet, collect
from .scoring import HumanReviewPending, extract_answer
from .skills import copy_skills, load_skills
from .workspace import Workspace, resolve_workspace

log = logging.getLogger("wikiskill")
# bundled examples sit next to the package (repository root, or the skill folder made by tools/integrate.py)
EXAMPLES = {p.name: p for p in (Path(__file__).resolve().parent.parent / "examples").glob("*")
            if (p / "suite.json").is_file()}
EXIT_ERROR, EXIT_CONFIG, EXIT_REVIEW = 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not be UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        return args.func(args) or 0
    except HumanReviewPending as e:
        print(f"\nHuman review needed: open {e.sheet}, fill in the 'verdict' column "
              f"(OK / NG / 0-1) for {len(e.missing)} row(s), save it, and run the same command again.")
        return EXIT_REVIEW
    except (ConfigError, ValueError, FileNotFoundError) as e:  # fix the input, then run again
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except (RuntimeError, OSError) as e:  # CLI/model failures, locks, disk errors: run again to resume
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wikiskill", description="WikiSkill: evolve agent skills with a persistent wiki.")
    parser.add_argument("--version", action="version", version=f"wikiskill {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_run_commands(sub)
    _add_helper_commands(sub)
    return parser


def _add_run_commands(sub: Any) -> None:
    """Commands that run agents: check, evolve, improve, evaluate, review."""
    p = sub.add_parser("check", help="check Python, the CLI backend and (optionally) a suite")
    _backend_args(p)
    p.add_argument("--suite", type=_path, help="also validate this suite")
    p.add_argument("--no-call", action="store_true", help="do not make test calls to the model")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("evolve", help="run the WikiSkill loop (resumes if the workspace exists)")
    p.add_argument("suite", type=_path)
    p.add_argument("--workspace", type=_path, required=True)
    _backend_args(p)
    _run_args(p)
    defaults = EvolveOptions()
    p.add_argument("--iterations", type=int, default=defaults.iterations, help="K (default %(default)s)")
    p.add_argument("--seed", type=int, default=defaults.seed)
    p.add_argument("--max-turns", type=int, default=defaults.max_turns, help="Skill Proposer turn limit")
    p.set_defaults(func=cmd_evolve)

    p = sub.add_parser("improve", help="keep improving: resume, or add pending feedback as tasks and evolve on")
    p.add_argument("suite", type=_path)
    p.add_argument("--workspace", type=_path, required=True, help="workspace folder (kept across rounds)")
    _backend_args(p)
    _run_args(p)
    p.add_argument("--iterations", type=int, default=3, help="iterations per round (default %(default)s)")
    p.add_argument("--min-new", type=int, default=MIN_NEW,
                   help="pending feedback rows needed to start a new round (default %(default)s)")
    p.add_argument("--export", type=_path, help="also copy the skills here, e.g. ~/.claude/skills")
    p.set_defaults(func=cmd_improve)

    p = sub.add_parser("evaluate", help="score a split with evolved skills (or none)")
    p.add_argument("suite", type=_path)
    p.add_argument("--out", type=_path, required=True, help="output folder (report.json, runs/)")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--workspace", type=_path, help="use the skills of this workspace")
    source.add_argument("--skills", type=_path, help="use the skills in this folder")
    source.add_argument("--no-skills", action="store_true", help="baseline without skills")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    _backend_args(p)
    _run_args(p)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("review", help="hybrid mode: run tasks once and write a sheet for a person to check")
    p.add_argument("suite", type=_path)
    p.add_argument("--out", type=_path, required=True)
    p.add_argument("--splits", default="train,val,test", help="comma separated (default %(default)s)")
    _backend_args(p)
    p.add_argument("--workers", type=int, default=EvolveOptions().workers, help="parallel agent runs")
    p.set_defaults(func=cmd_review)


def _add_helper_commands(sub: Any) -> None:
    """Commands that only read or write files: compare, review-apply, feedback, status, export."""
    p = sub.add_parser("compare", help="paired bootstrap test between two report.json files (B minus A)")
    p.add_argument("report_a", type=_path)
    p.add_argument("report_b", type=_path)
    p.add_argument("--iterations", type=int, default=1000)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("review-apply", help="hybrid mode: copy reviewed answers/rubrics into the tasks file")
    p.add_argument("suite", type=_path)
    p.add_argument("sheet", type=_path)
    p.set_defaults(func=cmd_review_apply)

    p = sub.add_parser("init", help="start a suite from a bundled example and copy input files into it")
    p.add_argument("suite", type=_path, help="new suite folder")
    p.add_argument("--like", choices=sorted(EXAMPLES), required=True, help="example to start from")
    p.add_argument("--inputs", type=_path, help="folder whose files become the suite's input files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("feedback", help="queue a judged real-world result for the next `improve`")
    p.add_argument("suite", type=_path)
    p.add_argument("--verdict", choices=["good", "bad"], required=True)
    p.add_argument("--prompt", required=True, help="the request the user made")
    p.add_argument("--output", help="the result that was produced (required for good)")
    p.add_argument("--correct", help="the correct result, when the user gave one")
    p.add_argument("--note", help="the user's reason or comment")
    p.add_argument("--file", type=_path, action="append", default=[], help="input file of the request (repeatable)")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("status", help="show the progress of a workspace")
    p.add_argument("workspace", type=_path)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="copy evolved skills into a Claude Code / Codex skills folder")
    p.add_argument("workspace", type=_path)
    p.add_argument("--dest", type=_path, required=True,
                   help="e.g. .claude/skills (Claude Code) or .agents/skills (Codex)")
    p.add_argument("--force", action="store_true", help="overwrite skills that already exist in --dest")
    p.add_argument("--suite", type=_path, help="add the feedback footer for this suite (as `improve --export` does)")
    p.set_defaults(func=cmd_export)


def _path(value: str) -> Path:
    return Path(value).expanduser()  # PowerShell and quoted paths leave ~ unexpanded


def _backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=["claude", "codex"], default="claude", help="default %(default)s")
    p.add_argument("--model", help="model name passed to the CLI (default: the CLI's own default)")
    p.add_argument("--judge-model", help="model for the llm_judge scorer (default: --model)")
    p.add_argument("--backend-config", type=_path, help="JSON file overriding CLI flags (docs/CONFIGURATION.md)")


def _run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workers", type=int, default=EvolveOptions().workers, help="parallel agent runs")
    p.add_argument("--human-review", choices=["on", "off"],
                   help="override the suite's human_review setting (on: a person scores every result)")


@contextlib.contextmanager
def _file_log(path: Path) -> Iterator[None]:
    """Also log to `path`; the handler is closed afterwards (Windows locks open files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        yield
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _backends(args: argparse.Namespace):
    backend = make_backend(args.backend, args.model, args.backend_config)
    judge = make_backend(args.backend, args.judge_model, args.backend_config) if args.judge_model else backend
    return backend, judge


def _suite(args: argparse.Namespace, require_answers: bool = True):
    human = getattr(args, "human_review", None)
    return load_suite(args.suite, require_answers, None if human is None else human == "on")


# ----- commands --------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    print(f"wikiskill {__version__} on Python {platform.python_version()} ({platform.system()})")
    if sys.version_info < (3, 9):  # noqa: UP036 -- a clear message for users who start an old Python
        print("error: Python 3.9 or newer is required", file=sys.stderr)
        return EXIT_CONFIG
    if args.suite:
        suite = load_suite(args.suite, require_answers=False)
        counts = {s: len(suite.split(s)) for s in ("train", "val", "test")}
        print(f"suite '{suite.name}': {counts}, scorer={suite.scorer['type']}, human_review={suite.human_review}")
        load_suite(args.suite)  # strict check: answers present for the scorer
    backend, _ = _backends(args)
    exe = shutil.which(backend.config.command)
    print(f"backend {backend.name}: command {backend.config.command!r} -> {exe or 'NOT FOUND'}")
    if not exe:
        return EXIT_CONFIG
    if args.no_call:
        return 0
    reply = backend.complete("Reply with exactly the word OK and nothing else.")
    print(f"  complete(): {reply.strip()[:80]!r}")
    with tempfile.TemporaryDirectory(prefix="wikiskill-check-") as tmp:
        Path(tmp, "numbers.txt").write_text("17\n25\n", encoding="utf-8")
        result = backend.run_agent("Add the numbers in numbers.txt using a shell command. "
                                   "Give the result inside <answer></answer>.", None, Path(tmp))
    answer = extract_answer(result.final_text)
    print(f"  run_agent(): ok={result.ok} answer={answer!r} (expected '42')" + (f" error={result.error}" if not result.ok else ""))
    return 0 if result.ok and answer.strip() == "42" else EXIT_ERROR


def cmd_evolve(args: argparse.Namespace) -> int:
    suite = _suite(args)
    backend, judge = _backends(args)
    ws = resolve_workspace(args.workspace)
    opts = EvolveOptions(iterations=args.iterations, workers=args.workers, seed=args.seed, max_turns=args.max_turns)
    with _file_log(ws.root / "run.log"):
        log.info("evolve: suite=%s backend=%s workspace=%s human_review=%s", suite.name, backend.name, ws.root,
                 suite.human_review)
        state = Evolution(suite, backend, ws, opts, judge).run()
    _print_status(state)
    print(f"\nskills: {ws.skills}\nwiki:   {ws.wiki}")
    return 0


def cmd_improve(args: argparse.Namespace) -> int:
    suite = _suite(args, require_answers=False)  # checked strictly in _evolve_round, after the merge
    ws = resolve_workspace(args.workspace)
    state = ws.load_state()
    merged = merge_ready(suite.root, suite.tasks_path, state, args.iterations, args.min_new)
    if merged:
        suite = _suite(args, require_answers=False)
    if has_work(state, suite, args.iterations):
        _evolve_round(args, ws, merged)
    else:
        print(f"nothing to do: {len(pending(suite.root))} feedback row(s) pending; "
              f"a new round starts at {args.min_new}")
    if args.export:  # also when there is nothing to do, so a missed or manual export is brought up to date
        for message in export_skills(ws.skills, args.export, suite.root):
            print(message)
    return 0


def _evolve_round(args: argparse.Namespace, ws: Workspace, merged: int) -> None:
    suite = _suite(args)  # strict check now that the feedback is merged
    backend, judge = _backends(args)
    opts = EvolveOptions(iterations=args.iterations, workers=args.workers, adopt_new_tasks=True)
    with _file_log(ws.root / "run.log"):
        log.info("improve: suite=%s workspace=%s merged_feedback=%d", suite.name, ws.root, merged)
        state = Evolution(suite, backend, ws, opts, judge).run()
    _print_status(state)
    print(f"\nmerged feedback: {merged}\nskills: {ws.skills}")


def cmd_evaluate(args: argparse.Namespace) -> int:
    suite = _suite(args)
    backend, judge = _backends(args)
    skills_dir = resolve_workspace(args.workspace).skills if args.workspace else (None if args.no_skills else args.skills)
    if skills_dir is not None and not skills_dir.is_dir():
        raise FileNotFoundError(f"skills folder not found: {skills_dir}")
    with _file_log(args.out / "run.log"):
        report = evaluate(suite, backend, skills_dir, args.split, args.out, args.workers, judge)
    print(f"{report['split']}: mean score {report['mean_score']:.4f} over {report['n_tasks']} tasks "
          f"(skills: {', '.join(report['skills']) or 'none'})\nreport: {args.out / 'report.json'}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    result = compare_reports(args.report_a, args.report_b, args.iterations)
    print(json.dumps(result, indent=2))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    suite = _suite(args, require_answers=False)
    backend, _ = _backends(args)
    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    sheet = collect(suite, backend, splits, args.out, args.workers)
    print(f"review sheet: {sheet}\nFill in verdict (OK/NG), corrected_answer for NG rows and optional "
          f"rubric_note, then run: python -m wikiskill review-apply {args.suite} {sheet}")
    return 0


def cmd_review_apply(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite, require_answers=False)
    counts = apply_sheet(suite, args.sheet)
    print(f"updated {suite.tasks_path} (backup: {suite.tasks_path.name}.bak): {counts}")
    if counts["ng_without_correction"]:
        print("note: some NG rows have no corrected_answer; those tasks still have no reference answer.")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    if (args.suite / "suite.json").exists():
        raise ConfigError(f"{args.suite / 'suite.json'} already exists")
    if args.inputs is not None and not args.inputs.is_dir():
        raise FileNotFoundError(f"--inputs {args.inputs} is not a folder")
    example = EXAMPLES[args.like]
    args.suite.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(example / "suite.json"), str(args.suite / "suite.json"))
    files_dir = args.suite / "files"
    if args.inputs is not None:
        shutil.copytree(str(args.inputs), str(files_dir), dirs_exist_ok=True)
    files_dir.mkdir(exist_ok=True)
    files = sorted(p.relative_to(files_dir).as_posix() for p in files_dir.rglob("*") if p.is_file())
    print(f"created {args.suite} from {example}\nnext: edit suite.json and write tasks.jsonl "
          f"(example tasks: {example / 'tasks.jsonl'})\ninput files ({len(files)}): {', '.join(files[:20])}"
          + (" ..." if len(files) > 20 else ""))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite, require_answers=False)
    task_id, count = record_feedback(suite, args.prompt, args.verdict, args.output, args.correct, args.note,
                                     args.file)
    print(f"recorded {task_id}; {count} feedback row(s) wait for the next `improve`"
          + ("\nready: run `improve` to learn from them" if count >= MIN_NEW else ""))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws = resolve_workspace(args.workspace)
    state = ws.load_state()
    if state is None:
        print(f"{args.workspace} has no state.json (not started)")
        return EXIT_CONFIG
    _print_status(state)
    print("active skills:", ", ".join(n for n, _ in load_skills(ws.skills)) or "(none)")
    return 0


def _print_status(state: Any) -> None:
    print(f"\nsuite {state['suite']} | baseline val {_fmt(state['baseline'])} | best val {_fmt(state['r_best'])} "
          f"| next iteration {state['iteration']} (stage {state['stage']})")
    for h in state["history"]:
        print(f"  iter {h['iteration']:>2}: {h['outcome']:<9} {h['action'] or '':<9} {h['target'] or '':<30} "
              f"val {_fmt(h['val_score'])}")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def cmd_export(args: argparse.Namespace) -> int:
    footer = feedback_footer(load_suite(args.suite, require_answers=False).root) if args.suite else ""
    copied = copy_skills(resolve_workspace(args.workspace).skills, args.dest, lambda target: args.force, footer)
    if not copied:
        print("no skills to export")
        return EXIT_CONFIG
    for name, target, written in copied:
        print(f"exported {name} -> {target}" if written else f"skip {name}: {target} exists (use --force to overwrite)")
    return 0
