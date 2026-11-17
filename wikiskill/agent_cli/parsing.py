"""Read what the CLIs print: final text, a readable trace, usage and the error, if any.

claude: `--output-format json` prints one result object, `stream-json` one event per line
        (assistant / user messages with content blocks, system events, and a final result event).
codex:  `exec --json` prints one event per line (item.completed, turn.completed, turn.failed, error).

Events and fields these functions do not know are skipped, so a newer CLI that adds some still parses.
Nothing here raises on bad output: a cut-off or empty stream comes back as an `incomplete` error.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import CallError, classify


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None  # as reported by the CLI; None when it reports no cost (codex)


@dataclass(frozen=True)
class Outcome:
    text: str = ""  # the final answer
    trace: str = ""  # the trajectory as Markdown
    usage: Usage | None = None  # None when the output held no usage (a cut-off call, an early failure)
    error: CallError | None = None
    finished: bool = False  # the CLI reported the end of the turn (with or without an error)
    cli_version: str | None = None  # when the output names it (claude's system/init event)
    meta: dict[str, Any] = field(default_factory=dict)  # num_turns, credential_expires_at, ... when present


def parse_output(kind: str, mode: str, stdout: str) -> Outcome:
    if kind == "claude":
        return _parse_claude(mode, stdout)
    if kind == "codex":
        return _parse_codex(stdout)
    raise ValueError(f"unknown CLI {kind!r}")


def json_lines(stdout: str) -> list[dict]:
    """The JSON objects among the lines of `stdout`; other lines (warnings, cut-off JSON) are skipped."""
    events = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(data: Any, key: str) -> int:
    value = data.get(key) if isinstance(data, dict) else None
    return int(value) if _number(value) else 0


def _block(text: str) -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}\n{text.rstrip()}\n{fence}"


# ----- claude ----------------------------------------------------------------------
def _claude_events(stdout: str) -> list[dict]:
    try:  # `--output-format json` prints one object (a list of events with --verbose)
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return json_lines(stdout)
    items = data if isinstance(data, list) else [data]
    return [e for e in items if isinstance(e, dict)]


def _parse_claude(mode: str, stdout: str) -> Outcome:
    events = _claude_events(stdout)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    version = init.get("claude_code_version") if isinstance(init.get("claude_code_version"), str) else None
    trace = "\n\n".join(filter(None, (_render_claude_event(e) for e in events)))
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None and mode == "complete" and len(events) == 1 and "result" in events[0]:
        result = events[0]  # the one object of `--output-format json`, whatever its type says
    if result is None:
        message = (f"claude returned non-JSON output: {stdout[:300]!r}" if mode == "complete" and not events
                   else "claude stream ended without a result event")
        return Outcome(trace=trace, error=CallError("incomplete", message), cli_version=version)
    cost = result.get("total_cost_usd")
    tokens = result.get("usage")
    usage = Usage(_int(tokens, "input_tokens"), _int(tokens, "output_tokens"),
                  _int(tokens, "cache_read_input_tokens"), _int(tokens, "cache_creation_input_tokens"),
                  float(cost) if _number(cost) else None) if _number(cost) or isinstance(tokens, dict) else None
    meta = {key: result[key] for key in ("num_turns", "duration_ms", "subtype", "terminal_reason",
                                         "credential_expires_at") if result.get(key) is not None}
    text = result.get("result")
    error = None
    failed = result.get("is_error") or str(result.get("subtype", "")).startswith("error_max_budget")
    if failed or (mode == "complete" and not isinstance(text, str)):  # API / quota / auth failures
        category = next((e["error"] for e in reversed(events)
                         if e.get("type") == "assistant" and isinstance(e.get("error"), str)), None)
        status = result.get("api_error_status")
        error = classify(str(text)[:500], category, status if _number(status) else None, result.get("subtype"))
    return Outcome("" if text is None else str(text), trace, usage, error, True, version, meta)


def _render_claude_event(event: dict) -> str:
    kind = event.get("type")
    if kind not in ("assistant", "user"):
        return ""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return f"**{kind}:** {content}" if content else ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text", "").strip():
            parts.append(f"**{kind}:** {block['text'].strip()}")
        elif btype == "thinking" and block.get("thinking", "").strip():
            parts.append(f"**reasoning:** {block['thinking'].strip()}")
        elif btype == "tool_use":
            payload = json.dumps(block.get("input", {}), ensure_ascii=False, indent=1)
            parts.append(f"**tool call `{block.get('name')}`:**\n{_block(payload)}")
        elif btype == "tool_result":
            out = block.get("content")
            if isinstance(out, list):
                out = "\n".join(str(c.get("text", c)) for c in out if isinstance(c, dict))
            label = "tool error" if block.get("is_error") else "tool output"
            parts.append(f"**{label}:**\n{_block(str(out))}")
    return "\n\n".join(parts)


# ----- codex -----------------------------------------------------------------------
def _parse_codex(stdout: str) -> Outcome:
    events = json_lines(stdout)
    if not events:
        return Outcome(error=CallError("incomplete", f"codex returned no JSON events: {stdout[:300]!r}"))
    items = [e["item"] for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)]
    messages = [str(i.get("text", "")) for i in items if i.get("type") == "agent_message"]
    trace = "\n\n".join(filter(None, (_render_codex_item(i) for i in items)))
    completed = next((e for e in reversed(events) if e.get("type") == "turn.completed"), None)
    failed = next((e for e in events if e.get("type") == "turn.failed"), None)
    tokens = (completed or {}).get("usage")
    usage = Usage(_int(tokens, "input_tokens"), _int(tokens, "output_tokens"),
                  _int(tokens, "cached_input_tokens")) if isinstance(tokens, dict) else None
    error = None
    if failed or not completed:
        detail = (failed or {}).get("error")
        message = detail.get("message") if isinstance(detail, dict) else detail
        if not message:  # the stream's own error events (the same text as turn.failed when both exist)
            message = next((e.get("message") for e in reversed(events) if e.get("type") == "error"), None)
        text = str(message or "turn did not complete")[:500]
        error = classify(text) if message else CallError("incomplete", text)
    return Outcome(messages[-1] if messages else "", trace, usage, error, bool(completed or failed))


def _render_codex_item(item: dict) -> str:
    itype = item.get("type")
    if itype == "agent_message":
        return f"**assistant:** {str(item.get('text', '')).strip()}"
    if itype == "reasoning":
        return f"**reasoning:** {str(item.get('text', '')).strip()}"
    if itype == "command_execution":
        return (f"**tool call `shell`:**\n{_block(str(item.get('command')))}\n\n"
                f"**tool output (exit {item.get('exit_code')}):**\n{_block(str(item.get('aggregated_output', '')))}")
    if itype == "error":
        return ""  # CLI warnings (hooks, model metadata) are not part of the agent's trajectory
    payload = {k: v for k, v in item.items() if k != "id"}
    return f"**{itype}:**\n{_block(json.dumps(payload, ensure_ascii=False, indent=1))}"
