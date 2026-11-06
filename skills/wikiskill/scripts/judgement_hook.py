"""UserPromptSubmit hook: remind Claude to record the user's judgement of the last result.

Claude Code runs this before every prompt (tools/integrate.py registers it). When the prompt looks
like a judgement ("良い", "違う、正しくは…", "wrong, it should be…"), it adds a short note to
Claude's context pointing at section B of the wikiskill skill; Claude still decides whether the
result belongs to a recurring task. Otherwise it prints nothing. It never blocks a prompt: any
error ends quietly with exit code 0. Kept to the standard library and old Python syntax, and
does not import the `wikiskill` package, so it adds only the interpreter start to each prompt.
"""
import json
import re
import sys

MAX_CHARS = 2000  # longer prompts are new requests, not a judgement
JUDGEMENT = re.compile(
    r"良い|良かった|いいね|いい感じ|完璧|ばっちり|バッチリ|その通り|合って(?:る|います)|正しい|正解"
    r"|違う|違い?ます|ちがう|間違|誤り|正しくは|じゃなくて|ダメ(?!ージ)|だめ|やり直し|おかしい"
    # English words count next to kana too ("wrongです"): only Latin letters may not touch them
    r"|(?<![A-Za-z])(?:good|great|perfect|correct|wrong|incorrect)(?![A-Za-z])|should be|not right|that'?s right",
    re.IGNORECASE,
)
NOTE = (
    "[wikiskill] この発言は直前の結果への評価（良い／違う）かもしれません。"
    "評価であり、その結果が依頼文と添付ファイルだけで解き直せる繰り返しの業務なら、"
    "wikiskill スキルの「B. 利用者の評価を記録する」に従って記録してください。"
    "そうでなければこの注意は無視し、利用者にも触れないでください。"
)


def note_for(prompt):
    """The context to add for `prompt`, or None."""
    text = prompt.strip()
    if not text or len(text) > MAX_CHARS or text.startswith("/") or not JUDGEMENT.search(text):
        return None
    return NOTE


def main():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")  # Claude Code sends UTF-8 on every system
        event = json.loads(raw or "{}")
        note = note_for(str(event.get("prompt") or ""))
        if note:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": note}}))  # ASCII: safe on any console code page
    except Exception:  # noqa: BLE001 -- a reminder must never stop the user's prompt
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
