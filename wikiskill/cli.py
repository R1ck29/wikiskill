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
from pathlib import Path
from typing import Any, Iterator, List, Optional

from . import __version__
from .backends import make_backend
from .config import ConfigError, load_suite
from .evaluate import compare_reports, evaluate
from .evolve import EvolveOptions, Evolution
from .review import apply_sheet, collect
from .scoring import HumanReviewPending, extract_answer
from .skills import load_skills
from .util import write_text
from .workspace import Workspace

log = logging.getLogger("wikiskill")
EXIT_ERROR, EXIT_CONFIG, EXIT_REVIEW = 1, 2, 3


def main(argv: Optional[List[str]] = None) -> int:
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
    """Commands that run agents: check, evolve, evaluate, review."""
    p = sub.add_parser("check", help="check Python, the CLI backend and (optionally) a suite")
    _backend_args(p)
    p.add_argument("--suite", type=Path, help="also validate this suite")
    p.add_argument("--no-call", action="store_true", help="do not make test calls to the model")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("evolve", help="run the WikiSkill loop (resumes if the workspace exists)")
    p.add_argument("suite", type=Path)
    p.add_argument("--workspace", type=Path, required=True)
    _backend_args(p)
    _run_args(p)
    defaults = EvolveOptions()
    p.add_argument("--iterations", type=int, default=defaults.iterations, help="K (default %(default)s)")
    p.add_argument("--seed", type=int, default=defaults.seed)
    p.add_argument("--max-turns", type=int, default=defaults.max_turns, help="Skill Proposer turn limit")
    p.set_defaults(func=cmd_evolve)

    p = sub.add_parser("evaluate", help="score a split with evolved skills (or none)")
    p.add_argument("suite", type=Path)
    p.add_argument("--out", type=Path, required=True, help="output folder (report.json, runs/)")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--workspace", type=Path, help="use the skills of this workspace")
    source.add_argument("--skills", type=Path, help="use the skills in this folder")
    source.add_argument("--no-skills", action="store_true", help="baseline without skills")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    _backend_args(p)
    _run_args(p)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("review", help="hybrid mode: run tasks once and write a sheet for a person to check")
    p.add_argument("suite", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--splits", default="train,val,test", help="comma separated (default %(default)s)")
    _backend_args(p)
    p.add_argument("--workers", type=int, default=EvolveOptions().workers, help="parallel agent runs")
    p.set_defaults(func=cmd_review)


def _add_helper_commands(sub: Any) -> None:
    """Commands that only read or write files: compare, review-apply, status, export."""
    p = sub.add_parser("compare", help="paired bootstrap test between two report.json files (B minus A)")
    p.add_argument("report_a", type=Path)
    p.add_argument("report_b", type=Path)
    p.add_argument("--iterations", type=int, default=1000)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("review-apply", help="hybrid mode: copy reviewed answers/rubrics into the tasks file")
    p.add_argument("suite", type=Path)
    p.add_argument("sheet", type=Path)
    p.set_defaults(func=cmd_review_apply)

    p = sub.add_parser("status", help="show the progress of a workspace")
    p.add_argument("workspace", type=Path)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="copy evolved skills into a Claude Code / Codex skills folder")
    p.add_argument("workspace", type=Path)
    p.add_argument("--dest", type=Path, required=True,
                   help="e.g. .claude/skills (Claude Code) or .agents/skills (Codex)")
    p.add_argument("--force", action="store_true", help="overwrite skills that already exist in --dest")
    p.set_defaults(func=cmd_export)


def _backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=["claude", "codex"], default="claude", help="default %(default)s")
    p.add_argument("--model", help="model name passed to the CLI (default: the CLI's own default)")
    p.add_argument("--judge-model", help="model for the llm_judge scorer (default: --model)")
    p.add_argument("--backend-config", type=Path, help="JSON file overriding CLI flags (docs/CONFIGURATION.md)")


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
    if sys.version_info < (3, 9):
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
    ws = Workspace(args.workspace)
    ws.init()
    opts = EvolveOptions(iterations=args.iterations, workers=args.workers, seed=args.seed, max_turns=args.max_turns)
    with _file_log(ws.root / "run.log"):
        log.info("evolve: suite=%s backend=%s workspace=%s human_review=%s", suite.name, backend.name, ws.root,
                 suite.human_review)
        state = Evolution(suite, backend, ws, opts, judge).run()
    _print_status(state)
    print(f"\nskills: {ws.skills}\nwiki:   {ws.wiki}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    suite = _suite(args)
    backend, judge = _backends(args)
    skills_dir = Workspace(args.workspace).skills if args.workspace else (None if args.no_skills else args.skills)
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


def cmd_status(args: argparse.Namespace) -> int:
    state = Workspace(args.workspace).load_state()
    if state is None:
        print(f"{args.workspace} has no state.json (not started)")
        return EXIT_CONFIG
    _print_status(state)
    print("active skills:", ", ".join(n for n, _ in load_skills(Workspace(args.workspace).skills)) or "(none)")
    return 0


def _print_status(state: Any) -> None:
    print(f"\nsuite {state['suite']} | baseline val {_fmt(state['baseline'])} | best val {_fmt(state['r_best'])} "
          f"| next iteration {state['iteration']} (stage {state['stage']})")
    for h in state["history"]:
        print(f"  iter {h['iteration']:>2}: {h['outcome']:<9} {h['action'] or '':<9} {h['target'] or '':<30} "
              f"val {_fmt(h['val_score'])}")


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.4f}"


def cmd_export(args: argparse.Namespace) -> int:
    skills = load_skills(Workspace(args.workspace).skills)
    if not skills:
        print("no skills to export")
        return EXIT_CONFIG
    dest = args.dest.expanduser()  # PowerShell does not expand ~ for external commands
    for name, text in skills:
        target = dest / name / "SKILL.md"
        if target.exists() and not args.force:
            print(f"skip {name}: {target} exists (use --force to overwrite)")
            continue
        write_text(target, text)
        print(f"exported {name} -> {target}")
    return 0
