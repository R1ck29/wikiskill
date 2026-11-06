# このフォルダで作業するエージェントへ

WikiSkill は、業務の例からエージェント用スキル（SKILL.md）を自動で育てるツールです。Python 3.9 以上の標準ライブラリだけで動き、
モデルは `claude`（または `codex`）コマンド経由でだけ呼びます。仕組みは [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、
使い方は [README.md](README.md) にあります。

## 組み込み（インストール）を頼まれたら

既定の入れ先は**利用者のグローバル設定 `~/.claude/skills`**（全プロジェクト共通）です。利用者がプラグインや
プロジェクトのフォルダをはっきり指定したときだけ、そこに入れます。
入れるのは `skills/wikiskill/` に本体・docs・examples を同梱した 1 フォルダで、`tools/integrate.py` が作ります。
**このフォルダのファイルを手で選んでコピーしたり書き換えたりせず**、次の順に進めてください。

1. 入れ先を決める。

   | 入れ先 | 使う場面・見分け方 | コマンドに渡すもの |
   |---|---|---|
   | グローバル設定（既定） | 指定がない・「グローバル」「自分の設定」「既存のハーネス」 | `~/.claude/skills --skills-dir` |
   | プラグイン | 指定されたフォルダに `.claude-plugin/plugin.json` がある | そのフォルダ（`<プラグイン>/skills/wikiskill/` に入る） |
   | プロジェクト | 指定されたフォルダに `.claude/` がある | そのフォルダ（`<プロジェクト>/.claude/skills/wikiskill/` に入る） |
   | スキルを並べたフォルダ | 指定されたフォルダのサブフォルダごとに `SKILL.md` がある | そのフォルダと `--skills-dir` |

2. 実行する。`python3` が無ければ Windows は `py`、それ以外は `python` に変える。`--force` は以前に入れた
   WikiSkill だけを置き換え、ほかのフォルダは消さない（初回に付けても害はない）。評価を見落とさないためのフックも
   設定ファイルに登録される（ほかの設定は残る）。利用者がフックを望まないときだけ `--no-hook` を付ける。

   ```bash
   python3 "<このフォルダ>/tools/integrate.py" ~/.claude/skills --skills-dir --force
   ```

3. 表示された入れ先が意図どおり（グローバルなら利用者のホームフォルダの `.claude/skills/wikiskill`）か確かめ、
   `hook: UserPromptSubmit added to ...` が出たことも確かめる（`warning:` が出たら、その内容と「評価はスキルの説明文だけで拾うことになる」と伝える）。
   続けて表示された `check --no-call` を実行し、`claude` コマンドが `NOT FOUND` でないことを確かめる（モデルは呼ばない）。
   `NOT FOUND` なら、CLI のインストールか [docs/CONFIGURATION.md](docs/CONFIGURATION.md) 3 章の `command` 設定が必要と伝える。
4. 同じ名前の WikiSkill がプラグインとしても入っていると（`claude plugin list` に `wikiskill` がある）2 つ並んで紛らわしいので、
   利用者に確認してから `claude plugin disable wikiskill@wikiskill` で止める。
5. 利用者には「新しい会話で『この業務を覚えさせて』と話しかけるか、繰り返しの作業の結果に『良い』『違う、正しくは…』と言えば使える（`/wikiskill` でも呼べる。プラグインに入れたなら
   `/<プラグイン名>:wikiskill`）」と伝える。プラグインやプロジェクトが Git 管理なら、追加されたフォルダをそのままコミットしてよい
   （`__pycache__` は含まれず、業務データや育成記録はこの中ではなく `~/wikiskill-work/` に作られる）。

Python が実行できない環境では、`skills/wikiskill/` を入れ先にコピーし、その中へ `wikiskill/`・`docs/`・`examples/` の
3 フォルダもコピーすれば同じ形になります。

社内環境で確かめておくこと（詳細は [docs/CONFIGURATION.md](docs/CONFIGURATION.md) 4 章）:

- `claude -p "hello"` が単体で返答すれば、API キー・Bedrock・社内ゲートウェイのどの認証でもそのまま動く。
  API キーでは実際の料金がかかる（目安と上限は README の「費用」）。
- 管理者の設定（managed settings）で `bypassPermissions` が禁止されているとエージェント実行が失敗するので、
  設定ファイルで許可するツールを限定する（CONFIGURATION.md 3 章の例）。

## このリポジトリを開発するとき

- テスト（モデルを呼ばない）: `python3 -m unittest discover -s tests -t .`。Lint: `ruff check .`（CI は ruff 0.17.1）
- `wikiskill/` は Python 3.9・標準ライブラリだけで動く書き方にする。`skills/wikiskill/scripts/run_wikiskill.py` は古い Python でも起動できる書き方を保つ。
- 版を上げるときは `wikiskill/__init__.py` と `.claude-plugin/plugin.json` の `version` を揃える（テストが確認する）。
- 動作を変えたら README・docs・`skills/wikiskill/SKILL.md` の該当箇所も同じコミットで直す。同じ説明を複数の場所に書かず、詳しい説明は 1 か所に置いてリンクする。
