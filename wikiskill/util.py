"""Small shared helpers: UTF-8 file I/O, JSON extraction from LLM text, truncation."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

_FENCED_JSON = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}
_UMASK = os.umask(0)
os.umask(_UMASK)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def is_windows_reserved(name: str) -> bool:
    """True for names Windows cannot use as files (CON, nul.txt, com1.md, ...)."""
    return name.split(".")[0].lower() in WINDOWS_RESERVED


def _formula_like(value: str) -> bool:
    return value.lstrip("'").startswith(_FORMULA_START)


def csv_cell(value: str) -> str:
    """Prefix ' so spreadsheet apps show text that starts like a formula instead of running it."""
    return "'" + value if _formula_like(value) else value


def csv_value(cell: str) -> str:
    """Undo csv_cell exactly (also for text that itself starts with ')."""
    return cell[1:] if cell.startswith("'") and _formula_like(cell[1:]) else cell


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text atomically (temp file + rename), creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o666 & ~_UMASK  # mkstemp would leave 0600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def backup_once(path: Path) -> None:
    """Copy `path` to `<name>.bak` unless that exists, so the original from before the first change is kept."""
    backup = Path(path).with_name(Path(path).name + ".bak")
    if not backup.exists():
        shutil.copy2(str(path), str(backup))


def append_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def truncate(text: str, limit: int) -> str:
    """Cap text at `limit` chars, keeping the head and the tail (where final answers live)."""
    if len(text) <= limit:
        return text
    marker = f"\n\n[... {len(text) - limit} characters truncated ...]\n\n"
    keep = max(limit - len(marker), 0)
    head = keep * 3 // 5
    return text[:head] + marker + text[len(text) - (keep - head):]


def extract_json_object(text: str) -> dict:
    """Return the first JSON object found in LLM output (fenced block first, then raw scan)."""
    candidates = [m.group(1) for m in _FENCED_JSON.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for start in [i for i, ch in enumerate(candidate) if ch == "{"]:
            try:
                obj, _ = decoder.raw_decode(candidate, start)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON object found in model output")
