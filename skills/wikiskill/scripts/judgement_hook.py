"""UserPromptSubmit hook: remind Claude to record the user's judgement of the last result.

Claude Code runs this before every prompt (tools/integrate.py registers it). When the prompt looks
like a judgement in Japanese or English ("良い", "違う、正しくは…", "looks good", "wrong, it should
be…"), it adds a short note to Claude's context pointing at section B of the wikiskill skill;
Claude still decides whether the result belongs to a recurring task. Otherwise it prints nothing.
It never blocks a prompt: any error ends quietly with exit code 0. Kept to the standard library
and old Python syntax, and does not import the `wikiskill` package, so it adds only the
interpreter start to each prompt.
"""
import json
import re
import sys
import unicodedata

MAX_CHARS = 2000  # longer prompts are new requests, not a judgement
SHORT_CHARS = 30  # a bare "OK" or "nope" is a judgement only as a whole short message

# Matched after NFKC, so full-width ＯＫ and ｇｏｏｄ count. English words may sit next to kana
# ("wrongです") but not inside other words ("goodness"): only Latin letters may not touch them.
_EN = r"(?<![a-z])(?:{})(?![a-z])"
JUDGEMENT = re.compile("|".join([
    # Japanese, good
    r"良い|良かった|よかった|いいね|いい感じ|いいです|完璧|ばっちり|バッチリ|その通り|そのとおり|合って(?:る|います|ます)"
    r"|正しい|正解|素晴らしい|すばらしい|さすが|助かり?ます|助かった|ありがとう|これで(?:いい|よい|ok)|問題(?:ない|ありません)",
    # Japanese, wrong
    r"違う|違い?ます|ちがう|違って|間違|誤り|正しくは|じゃなくて|ではなくて|そうじゃな|ダメ(?!ージ)|だめ|やり直"
    r"|おかしい|惜しい|足りない|ずれて|直して|修正して",
    # English, good
    _EN.format(r"good|great|perfect|correct|nice|excellent|awesome|exactly|lgtm|well done|looks right"
               r"|that'?s right|spot on|thanks|thank you"),
    # English, wrong
    _EN.format(r"wrong|incorrect|not right|not quite|not correct|should be|should have been|it'?s actually"
               r"|mistake|that'?s not|not what i|try again|redo|bad|off by"),
    # both languages: NG
    _EN.format("ng"),
]), re.IGNORECASE)
SHORT = re.compile(r"^(?:ok(?:ay)?|オーケー|オッケー|おけ|よし|nope|yep|no)[\s!！.。、,]*$", re.IGNORECASE)
NOTE = (
    "[wikiskill] この発言は直前の結果への評価（良い／違う）かもしれません。"
    "評価であり、その結果が依頼文と添付ファイルだけで解き直せる繰り返しの業務なら、"
    "wikiskill スキルの「B. 利用者の評価を記録する」に従って記録してください。"
    "そうでなければこの注意は無視し、利用者にも触れないでください。"
)


def note_for(prompt):
    """The context to add for `prompt`, or None."""
    text = unicodedata.normalize("NFKC", prompt).strip()
    if not text or len(text) > MAX_CHARS or text.startswith("/"):
        return None
    if JUDGEMENT.search(text) or (len(text) <= SHORT_CHARS and SHORT.match(text)):
        return NOTE
    return None


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
