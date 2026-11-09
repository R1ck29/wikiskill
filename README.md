# WikiSkill

[![tests](https://github.com/R1ck29/wikiskill/actions/workflows/test.yml/badge.svg)](https://github.com/R1ck29/wikiskill/actions/workflows/test.yml)

AI エージェント（Claude Code / Codex）に同じ種類の業務タスクを繰り返し解かせ、その実行記録から
**「スキル」（エージェントが読む手順書 SKILL.md）を自動で育てる**ツールです。論文
[WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution (arXiv:2608.27454)](https://arxiv.org/abs/2608.27454)
の手法を、論文本文・Algorithm 1・付録のプロンプトに基づいて実装しています（論文にはコードへのリンクがないため独自実装です）。

- **Python 3.9 以上の標準ライブラリだけ**で動きます（`pip install` 不要）。Windows / macOS / Linux 対応。
- モデルは手元の **`claude`（または `codex`）コマンド経由でのみ**呼びます。WikiSkill 自身はネットワークに接続しないので、
  CLI が API キー・Bedrock・LLM ゲートウェイのどれで認証していても、そのまま使えます。
- 育ったスキルは Claude Code や Codex のスキルとしてそのまま使えます。

## しくみ

```
 1 反復の流れ
 ① 推論エージェント  学習用タスクを今のスキルで解く → 実行記録を保存
 ② Wiki メンテナ     成功・失敗の記録を分析 → 「パターン集（wiki）」を更新
 ③ スキル提案者      wiki と記録を読み、スキルを 1 つ作る/直す案を出す
 ④ 検証ゲート        検証用タスクで採点し、点が上がった時だけ採用（下がれば元に戻す）

 wiki は採用・不採用にかかわらず蓄積し続ける ＝ 失敗した試みも次の反復で活かされる
```

タスクは 3 つに分けて使います。**train**（学習用。①で解く）、**val**（検証用。④の採否判定）、
**test**（最終評価用。育成中は一切使わない）。分け方は WikiSkill が自動で決めるので、例を書くときに考える必要はありません
（決め方は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) の「タスクの自動振り分け」）。

育ったスキルを使い始めたあとも、利用者が結果に「良い」「違う、正しくは…」と言うか、「千円単位で」のように直しを指示するだけで記録され（`feedback`。違うときだけ言う使い方でも育ちます）、
3 件たまると裏で自動的に育て直します（`improve`。点が上がったときだけ採用し、書き出したスキルも更新）。
**例を用意していない業務も、普段の作業結果への評価から始められます**。Claude が業務ごとのスイートを作って評価を記録し、
6 件たまると最初の育成が自動で始まります（論文と同じくスキルも wiki も空から始め、評価された実際の依頼を train / val に使います）。
詳しい構造・自動改善の動き・論文との対応は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) にあります。

## 入れ方

どの方法でも、Python 3.9 以上と `claude`（または `codex`）コマンドが必要です（ターミナルで `claude -p "hello"` が返答すれば OK）。
入れたあとは Claude に「この業務を覚えさせて」「育成の進み具合は？」と話しかけるだけで、Claude が下のコマンドを代わりに実行します。
利用者の作業は**例のフォルダを伝えるか、普段の作業結果を「良い」「違う」と評価する（または直しを指示する）ことだけ**で、正解が無い業務は Claude が採点基準を書いて自動採点し、
育ったスキルは `~/.claude/skills` に書き出されて全プロジェクトで使えるようになります。
組み込むと、評価の見落としを防ぐフックも Claude Code の設定に登録されます。「良い」「違う、正しくは…」「looks good」のような評価や、
「いや、税込で」「もっと短く」「Make it shorter」のような直しの指示らしい発言（新しい依頼でも付くことがあります）にだけ、「評価なら記録して」という短い注意を Claude に渡します。
「税込にして」のように新しい依頼と見分けのつかない直しには付かず、スキルの説明文だけで Claude が判断します。
記録するかどうかは Claude が判断し、要らなければ `--no-hook` を付けて組み込み直すと外れます（詳細は [docs/CONFIGURATION.md](docs/CONFIGURATION.md) 4 章）。
消費するトークンの目安（文字数からの推定）は、常に 1 セッションあたり約 200（スキルの説明文）、呼び出したときだけ約 3,500（手順書）、フックの注意が付いた発言で約 150 です。

**A. ZIP から自分のグローバル設定に入れる（通常はこちら）**: GitHub の「Code → Download ZIP」で取得して展開し、
[INSTALL_PROMPT.md](INSTALL_PROMPT.md) の文章を Claude Code に貼り付けて送ります。Claude が [CLAUDE.md](CLAUDE.md) の手順に従って
`~/.claude/skills/wikiskill` に入れ、動作を確かめます。特定のプラグインやプロジェクトに入れたい場合の文章も同じファイルにあります。
手で行う場合は、展開したフォルダで次を実行します（`python` は macOS / Linux では `python3`、Windows では `py`）。

```bash
python tools/integrate.py ~/.claude/skills --skills-dir --force
```

**B. プラグインとして入れる（GitHub に接続できる環境）**: Claude Code のチャット欄に
`/plugin install wikiskill --marketplace R1ck29/wikiskill` と入力します（`/wikiskill:wikiskill` で呼び出せます）。
A と B を両方入れると同じスキルが 2 つ並ぶので、どちらか一方にしてください。B で入るフックは `python3` で起動するので、
`python3` が無い Windows では A を使ってください。

入れた後は、認証・ツールの許可・フック・自動の改善が導入先の環境で動くかを
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) の 5 章「導入先で確かめること」の順で確かめてください。

## 費用と API キー

API キーで使う場合は、モデルの呼び出しごとに実際の料金がかかります（サブスクリプションでログインした claude ではプランの利用枠を使います）。

- **育成 1 ラウンドの量**: 最初に val 件数分、その後 1 反復ごとに train＋val 件数分のエージェント実行があります。加えて
  Wiki メンテナが 1 回（応答が壊れていれば最大 3 回）、スキル提案者が最大 20 回のテキスト生成を行い、`llm_judge` 採点なら採点 1 件につき 1 回増えます。
  付属の例で 1 ラウンド（3 反復）の目安は、翻訳（train 4・val 3 件）が Sonnet 5.5 で約 1〜3 ドル、売上分析（train 8・val 6 件）が約 4〜12 ドルです
  （Opus 5.5 ならその約 2 倍。呼び出し回数からの見積もりで、実測は評価 6 件から始めた小さな業務の 1 反復で約 0.5 ドルの 1 回だけ）。手動の実行は `--daily-budget` を付けない限り上限がないので、まず少数のタスクと `--iterations 2` で試してください。
- **自動の改善**（`feedback` が起動する `improve`）: claude ではモデル `sonnet`（現在は Sonnet 5.5）を使い、業務（ワークスペース）ごとに**1 日 10 ドル**、
  すべての業務の合計で**1 日 20 ドル**までに抑えます（今日の額は `budget` で確認でき、合計の上限は `budget --total USD` で変えられます）。
  業務ごとの額は `improve --daily-budget USD` を一度実行すれば変わります。費用は CLI が報告する実際の額（`total_cost_usd`）で数えますが、
  上限に達した時点で動いている呼び出しや、タイムアウト・中断で結果が返らなかった呼び出しの分は数えられないため、少し超えることがあります。
  上限に達すると `paused` になり、翌日以降の最初の記録で続きから再開します。費用を報告しない codex では、業務ごとにも、すべての業務の合計でも 1 日 1 回にします。
- 自動の改善を止めたいときは、スイートの `improve.json` を消します。

## コマンドで使う

以降のコマンドは、リポジトリ（または展開した ZIP）のフォルダで実行します。`python` は macOS / Linux では `python3`、Windows では `py` に読み替えてください。
Claude に話しかけて使う場合は、この章を読む必要はありません。

### クイックスタート（付属サンプル）

```bash
python -m wikiskill check --model sonnet
```

`complete(): 'OK'` と `run_agent(): ok=True answer='42' sum.txt='42'` が出れば準備完了です（Codex は `--backend codex`）。
失敗したときの対処は [docs/CONFIGURATION.md](docs/CONFIGURATION.md) の 1 章にあります。

```bash
python -m wikiskill evolve examples/sales-analysis --workspace workspaces/sales --model haiku --iterations 4
```

売上 CSV の集計タスクでスキルを育てます。答えは「金額は税抜で報告する」という**データに書かれていない業務ルール**に
従っているため、エージェントは失敗から学ぶ必要があります。止めても同じコマンドで続きから再開します。

```bash
python -m wikiskill evaluate examples/sales-analysis --no-skills --out eval/base --model haiku
```

```bash
python -m wikiskill evaluate examples/sales-analysis --workspace workspaces/sales --out eval/skills --model haiku
```

```bash
python -m wikiskill compare eval/base/report.json eval/skills/report.json
```

test タスクでスキルなし／ありを比べ、差が偶然でないかを検定します（`p_value` が 0.05 未満なら有意）。

```bash
python -m wikiskill export workspaces/sales --dest ~/.claude/skills
```

育ったスキルを書き出します。書き出し先は使い方に合わせて選びます。

| 使い方 | `--dest` |
|---|---|
| Claude Code の全プロジェクトで使う | `~/.claude/skills`（`~` はホームフォルダ。Windows でもそのまま書けます） |
| Claude Code の特定プロジェクトで使う | `<そのプロジェクト>/.claude/skills` |
| Codex で使う | `~/.agents/skills` または `<そのプロジェクト>/.agents/skills` |

### 自分の業務で使う

1. 同じ種類の業務（例:「月次売上の集計」「契約書の英訳」）を 1 つの**スイート**（フォルダ）にまとめ、
   タスクと正解・採点方法を書きます。`python -m wikiskill init <新しいフォルダ> --like sales-analysis --inputs <入力ファイルのフォルダ>`
   で雛形を作れます。書き方と業務別の採点方法の選び方は [docs/SUITE_FORMAT.md](docs/SUITE_FORMAT.md) にあります。
2. `python -m wikiskill check --suite <フォルダ> --no-call` で形式を確認します。
3. `python -m wikiskill improve <フォルダ> --workspace <ワークスペース> --export ~/.claude/skills` で育てて書き出します
   （`evolve` と `export` を 1 度に行い、以後の `feedback` から自動で育て直す設定も保存します）。

例が無い業務は、`python -m wikiskill init <新しいフォルダ> --like translation-ja-en --workspace <ワークスペース> --export ~/.claude/skills`
でタスクの無いスイートを作り、`suite.json`（業務の説明と採点基準）だけを直します。あとは `feedback` で記録した結果が train / val になり、
6 件たまった時点で最初の `improve` が自動で始まります。依頼文と添付ファイルだけで解き直せる業務に限られ、
最初のうちは採否が偶然に左右されやすいなどの限界があります（[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) の「例が無い業務の始め方」）。
test はタスクが 40 件になるまで作られないので、それまでスキルあり／なしを比べるときは `evaluate` に `--split val` を付けます。

### 正解を人が決める（2 つのモード）

**ハイブリッド（推奨）**: 最初に 1 回だけ人がエージェントの出力を確認し、その結果を正解データにして以降は自動採点します。

```bash
python -m wikiskill review my-suite --out review
```

`review/review_sheet.csv` を Excel で開き、各行の `verdict` に `OK` か `NG` を入れます。NG の行は `corrected_answer` に
正しい答えを、必要なら `rubric_note` に「良い答えの条件」を書いて保存します。

```bash
python -m wikiskill review-apply my-suite review/review_sheet.csv
```

tasks.jsonl に正解と採点基準が書き込まれます（`tasks.jsonl.bak` には、WikiSkill が最初に書き換える前の版が残ります。
split の自動の書き足しも書き換えに入るので、その場合はそれより前の版です）。

**毎回人が判定**: `suite.json` に `"human_review": true` を書くか、`evolve` / `improve` / `evaluate` に `--human-review on` を付けます。
採点が必要になるたびにレビューシート（`evolve` / `improve` は `<ワークスペース>/reviews/*.csv`、`evaluate` は `<--out>/review.csv`）が作られて
処理が止まるので、`verdict` に `OK` / `NG` / 0〜1 の数値を入れて同じコマンドを再実行します。
シートの `answer_key` 列は判定した出力の目印なので編集・削除しないでください（出力が変わったタスクは判定し直しになります）。
`=` や `-` で始まる文字は Excel が数式として扱わないよう先頭に `'` が付いていますが、読み込み時に自動で外れます。
このモードのスイートは自動の改善を行いません（`feedback` は `ready` と表示して手動の `improve` を待ちます）。

### コマンド一覧

| コマンド | 用途 |
|---|---|
| `check` | Python・CLI・スイート（`--suite`）の確認。`--no-call` でモデルを呼ばない |
| `init SUITE --like 例 --inputs DIR` | 付属の例（`sales-analysis` / `translation-ja-en`）からスイートを作り、入力ファイルをコピー。`--workspace DIR`（と `--export DEST`）を付けると、タスクを書かずに `feedback` の記録だけで育て始める設定を保存 |
| `list` | `~/wikiskill-work`（`--root DIR` で変更）にあるスイートの業務名・説明・タスク数・未取り込みの記録・自動の改善の有無を表示 |
| `evolve SUITE --workspace DIR` | スキルを育てる。再実行で再開、`--iterations`（既定 8）を増やせば延長 |
| `improve SUITE --workspace DIR --export DEST` | 同じワークスペースで育て続ける。途中なら再開し、`feedback` の記録が `--min-new`（既定 3）件以上（まだ育てていないスイートでは何件でも。ただしタスクが 1 件も無いスイートは 6 件から）あればタスクに加えて `--iterations`（既定 3）回続け、なければ書き出しだけ行う。`--daily-budget USD` で 1 日の費用に上限、`--wait` で実行中の処理の終了を待つ |
| `feedback SUITE --verdict good\|bad --prompt 依頼` | 実際の利用結果を記録（`--output` 出した結果（good では必須）、`--correct` 正解、`--note` 理由、`--file` 入力ファイル）。記録がたまると `improve` を裏で起動し、`started` / `paused`（今日の上限に到達）/ `ready`（手動の `improve` が必要）と表示。前回の自動の改善が失敗していれば `warning` で原因を表示 |
| `evaluate SUITE --out DIR` と `--workspace DIR` / `--skills DIR` / `--no-skills` のどれか | 指定したスキルで採点し `report.json` を出力（`--split` 既定 test） |
| `compare A.json B.json` | 2 つの評価結果の差を検定 |
| `review` / `review-apply` | ハイブリッド方式のレビュー |
| `forget SUITE --last` / `--id fb-...` | 誤って記録した評価を取り消す（取り込み済みなら、改善の最中でないときだけ。次の `improve` はそれを除いた新しいラウンドから） |
| `budget [--total USD]` | 今日の自動の改善の費用と、全業務の合計の上限を表示（`--total` で上限を変更） |
| `status DIR` | 反復ごとの採否と検証スコアを表示 |
| `export DIR --dest DIR` | スキルを Claude Code / Codex のスキルフォルダへ書き出し（`--suite SUITE` で、結果の評価をそのスイートに記録する案内も付ける。同名のスキルがあれば `--force` で上書き） |

モデルを使うコマンド（`check`・`evolve`・`improve`・`evaluate`・`review`）では `--backend claude|codex`、`--model`、
`--judge-model`（llm_judge の採点モデル）、`--backend-config`（CLI の引数を変える。[docs/CONFIGURATION.md](docs/CONFIGURATION.md)）が使えます。
`evolve`・`improve`・`evaluate`・`review` には `--workers`（並列数、既定 4）もあります。全オプションは `python -m wikiskill <コマンド> --help` で確認できます。

| 終了コード | 意味 | 対処 |
|---|---|---|
| 0 | 成功 | |
| 1 | 実行時のエラー（CLI やモデルの失敗、同じワークスペースで実行中など） | 原因を解消して同じコマンドを再実行（完了分は再利用される。実行中のロックは、強制終了でもプロセスが終われば自動で外れる） |
| 2 | 入力・設定のエラー（スイートやオプションの誤り） | メッセージに従って修正 |
| 3 | 人のレビュー待ち | レビューシートを記入して再実行 |

### 出力（ワークスペース）

```
workspaces/sales/
  skills/<名前>/SKILL.md, PURPOSE.md   採用中のスキル（PURPOSE.md は根拠となったパターンと採用履歴）
  wiki/index.md                        パターンの目次（問題・原因・対策を 1 行ずつ）
  wiki/patterns/*.md                   失敗/成功パターンの詳細
  wiki/logs.md                         反復ごとの発見の記録
  wiki/skill-impact.md                 すべての提案の差分・検証スコア・採否
  raw/iter_NNN/traces/*.md             train の実行記録（書き換えない）
  runs/  reviews/  state.json  run.log 再開用データ、レビューシート、進捗、ログ
  background.log, background.err       自動の改善の出力と、起動直後のエラー
  spend.json                           上限つき（自動）の実行が今日使った費用
```

`python -m wikiskill status workspaces/sales` で進捗を確認できます。ワークスペースは業務データを含むので、
共有フォルダや Git に置かず、ローカルのドライブに置いてください（ネットワークドライブではファイルロックが使えないことがあります）。

## テスト

```bash
python -m unittest discover -s tests -t .
```

モデルを呼ばずに（偽の CLI で）全機能を確認します。GitHub Actions で Windows / macOS / Linux × Python 3.9 / 3.13 を実行しています。

## ドキュメント

- [INSTALL_PROMPT.md](INSTALL_PROMPT.md) / [CLAUDE.md](CLAUDE.md) — Claude に入れてもらうときの文章と、Claude が従う手順
- [docs/SUITE_FORMAT.md](docs/SUITE_FORMAT.md) — 自分の業務のタスクと採点方法の書き方
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — CLI の設定、導入先の環境での注意点と確かめること
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 仕組み、自動改善の動き、コード構成、論文との対応と差分

## ライセンス

独自に作成したコードとドキュメントは [MIT License](LICENSE) です。
`wikiskill/prompts/wiki_maintainer.md` と `wikiskill/prompts/skill_proposer.md` は、
WikiSkill 論文（arXiv:2608.27454）の付録 E.2 / E.3 を改変したもので、CC BY 4.0 が適用されます。
著者・出典・変更内容は [NOTICE](NOTICE) を参照してください。論文著者とは提携しておらず、推奨・承認を受けた実装ではありません。
