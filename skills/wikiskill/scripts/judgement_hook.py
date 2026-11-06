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
BRIEF_CHARS = 200  # words that new requests use too ("直して", "thanks") count only in a prompt this short
SHORT_CHARS = 30  # a bare "OK" or "nope" is a judgement only as a whole short message

# Matched after NFKC, so full-width ＯＫ and ｇｏｏｄ count. English words may sit next to kana
# ("wrongです") but not inside other words ("goodness"): only Latin letters may not touch them.
_EN = r"(?<![a-z])(?:{})(?![a-z])"
JUDGEMENT = re.compile("|".join([
    # Japanese, good / wrong
    r"良い|良かった|よかった|いいね|いい感じ|いいです|完璧|ばっちり|バッチリ|その通り|そのとおり|合って(?:る|います|ます)"
    r"|正しい|正解|素晴らしい|すばらしい|さすが|助かり?ます|助かった|これで(?:いい|よい|ok)|問題(?:ない|ありません)",
    r"違う|違い?ます|ちがう|違って|間違|誤り|正しくは|じゃなくて|ではなくて|そうじゃな|やり直|おかしい|惜しい|ずれて",
    # English, good / wrong
    _EN.format(r"good|perfect|correct|excellent|awesome|exactly|lgtm|well done|looks right|that'?s right|spot on"),
    _EN.format(r"wrong|incorrect|not right|not quite|not correct|should have been|it'?s actually|that'?s not"
               r"|not what i|off by"),
    _EN.format("ng"),  # both languages
]), re.IGNORECASE)
BRIEF = re.compile("|".join([
    r"ありがとう|ダメ(?!ージ)|だめ|足りない|直して|修正して",
    _EN.format(r"great|nice|thanks|thank you|should be|mistake|try again|redo|bad"),
    # corrections given as an instruction about the last result, without saying it was wrong
    # ("いや、税込で", "千円単位で切り捨てて", "そこは敬語で", "Make it shorter", "No, use thousands")
    # Only phrasings that point at the last result: generic words ("もっと詳しく説明して", "Make it work") are left out.
    r"^(?:いや|いえ|ううん)[、,\s]|(?:もっと|もう少し)(?:短|長|簡潔|丁寧|やさし|わかりやす|分かりやす|細か|大まか)"
    r"|単位で|単位に|抜けて|漏れて|多すぎ|少なすぎ|長すぎ|短すぎ|になってない|になっていない|入ってない|入っていない",
    r"^no[,、.]",
    _EN.format(r"instead[.!]?$|rather than|make it (?:shorter|longer|simpler|clearer|more|less|formal|polite)"
               r"|too (?:long|short|many|few)|needs to be|in thousands|shorter|longer"),
]), re.IGNORECASE)
SHORT = re.compile(r"^(?:ok(?:ay)?|オーケー|オッケー|おけ|よし|nope|yep|no)[\s!！.。、,]*$", re.IGNORECASE)
NOTE = (
    "[wikiskill] この発言は直前の結果への評価（良い／違う）か、結果の直しの指示かもしれません。"
    "評価または直しの指示であり、その結果が依頼文と添付ファイルだけで解き直せる繰り返しの業務なら、"
    "wikiskill スキルの「B. 利用者の評価を記録する」に従って記録してください。"
    "そうでなければこの注意は無視し、利用者にも触れないでください。"
)


def note_for(prompt):
    """The context to add for `prompt`, or None."""
    text = unicodedata.normalize("NFKC", prompt).strip()
    if not text or len(text) > MAX_CHARS or text.startswith("/"):
        return None
    if (JUDGEMENT.search(text) or (len(text) <= BRIEF_CHARS and BRIEF.search(text))
            or (len(text) <= SHORT_CHARS and SHORT.match(text))):
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
