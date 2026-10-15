# Claude に WikiSkill を入れてもらうプロンプト

ZIP（GitHub の「Code → Download ZIP」）を展開したら、Claude（Claude Code、または Claude Desktop の Code タブ）に
下の枠の文章をそのまま貼り付けて送ります。`<フォルダ>` は展開したフォルダ（`README.md` があるフォルダ）に置き換えます
（フォルダをチャット欄にドラッグするとパスが入ります）。

Claude がコマンドを 1 回だけ実行して、あなたの全プロジェクト共通のスキル置き場（`~/.claude/skills/wikiskill`）に入れます。
コマンドの実行許可を聞かれたら許可してください。入れたあとは ZIP のフォルダを消して構いません。
新しい版に入れ替えるときも同じ文章で上書きされます。

```text
<フォルダ> の WikiSkill を私のグローバル設定に入れて。次のコマンドを 1 回だけ実行し、ほかのファイルは読まないこと。
python3 "<フォルダ>/tools/integrate.py" ~/.claude/skills --skills-dir --force
Windows では python3 を py に変える。失敗したら理由を一言で答えて止まる。
成功したら「入りました。新しい会話で『この業務を覚えさせて』と話しかけてください」とだけ答える。
```
