"""Stand-in for the `claude` / `codex` executables in tests.

Records argv, stdin and cwd to $FAKE_CLI_RECORD (JSON lines) and prints output in the
format the real CLI would use. The reply text comes from $FAKE_CLI_REPLY.
"""
import io
import json
import os
import subprocess
import sys
import time


def record(path: str, entry: dict) -> None:
    """Append one JSON line. Parallel CLIs take turns through a lock file: on Windows an append
    is a seek plus a write, so two processes appending at once can overwrite each other's line."""
    lock = path + ".lock"
    deadline = time.monotonic() + 10
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    finally:
        os.close(fd)
        os.remove(lock)


def main() -> None:
    args = sys.argv[1:]
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read()
    reply = os.environ.get("FAKE_CLI_REPLY", "<answer>42</answer>")
    if reply == "__EMPTY__":
        reply = ""
    record(os.environ["FAKE_CLI_RECORD"], {"args": args, "stdin": stdin, "cwd": os.getcwd()})
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if os.environ.get("FAKE_CLI_HANG"):  # a grandchild keeps the output pipe open, like node behind a .cmd shim
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        time.sleep(60)
    if os.environ.get("FAKE_CLI_SILENT"):
        sys.exit(int(os.environ.get("FAKE_CLI_EXIT", "0")))
    if args and args[0] == "exec":
        events = [
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls",
                                                "aggregated_output": "data.csv\n", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": reply}},
            {"type": "turn.completed"},
        ]
        out.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    elif "stream-json" in args:
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "data.csv"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": reply, "total_cost_usd": 0.25},
        ]
        out.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    else:
        out.write(json.dumps({"type": "result", "is_error": False, "result": reply, "total_cost_usd": 0.5},
                             ensure_ascii=False))
    out.flush()
    sys.exit(int(os.environ.get("FAKE_CLI_EXIT", "0")))


if __name__ == "__main__":
    main()
