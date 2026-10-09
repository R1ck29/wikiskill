# WikiSkill

[![tests](https://github.com/R1ck29/wikiskill/actions/workflows/test.yml/badge.svg)](https://github.com/R1ck29/wikiskill/actions/workflows/test.yml)

AI エージェント（Claude Code / Codex）に同じ種類の業務タスクを繰り返し解かせ、その実行記録から
**「スキル」（エージェントが読む手順書 SKILL.md）を自動で育てる**ツールです。論文
[WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution (arXiv:2608.27454)](https://arxiv.org/abs/2608.27454)
の手法を、論文本文・Algorithm 1・付録のプロンプトに基づいて実装しています（論文にはコードへのリンクがないため独自実装です）。

- **Python 3.9 以上の標準ライブラリだけ**で動きます（`pip install` 不要）。Windows / macOS / Linux 対応。
- モデルは手元の **`claude` / `codex` コマンド経由でのみ**呼び出します。WikiSkill 自身はネットワークに接続しないため、
  CLI が Bedrock や社内ゲートウェイで認証済みなら、そのまま使えます。
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
**test**（最終評価用。育成中は一切使わない）。詳しい構造と論文との対応は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) にあります。

## 準備

1. **Python 3.9 以上**: ターミナルで `python3 --version`（Windows は `py --version`）を実行してバージョンを確認します。
   入っていなければ社内の手順で Python をインストールしてください。
2. **`claude` または `codex` コマンド**: `claude -p "hello"` または `codex exec "hello"` が返答すれば OK です。
3. **WikiSkill の入手**（どちらか）:
   - Git: `git clone https://github.com/R1ck29/wikiskill.git`
   - ZIP: GitHub の「Code → Download ZIP」で取得して展開。Windows の「すべて展開」では
     `wikiskill-main\wikiskill-main` のように二重フォルダになることがあるので、**`README.md` がある方のフォルダ**を使います。
4. **ターミナルを開いてそのフォルダへ移動**します（以降のコマンドはすべてこのフォルダで実行）。
   - Windows: エクスプローラーでフォルダを開き、アドレスバーに `powershell` と入力して Enter
   - macOS: ターミナル.app で `cd ` と入力してからフォルダをウィンドウにドラッグし、Enter

以降のコマンドは `python` と書いてあります。**macOS では `python3`、Windows では `py`** に読み替えてください。

## クイックスタート（付属サンプル）

```bash
python -m wikiskill check --model sonnet
```

`complete(): 'OK'` と `run_agent(): ok=True answer='42'` が出れば準備完了です（Codex は `--backend codex`）。

```bash
python -m wikiskill evolve examples/sales-analysis --workspace workspaces/sales --model haiku --iterations 4
```

売上 CSV の集計タスクでスキルを育てます。答えは「金額は税抜で報告する」という**データに書かれていない社内ルール**に
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

## 自分の業務で使う

1. 同じ種類の業務（例:「月次売上の集計」「契約書の英訳」）を 1 つの**スイート**（フォルダ）にまとめ、
   タスクと正解・採点方法を書きます。書き方と業務別の採点方法の選び方は [docs/SUITE_FORMAT.md](docs/SUITE_FORMAT.md) にあります。
2. `python -m wikiskill check --suite <フォルダ> --no-call` で形式を確認します。
3. 上のクイックスタートと同じ `evolve` → `evaluate` → `compare` → `export` の順に進めます。

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

tasks.jsonl に正解と採点基準が書き込まれます（最初の元ファイルは `tasks.jsonl.bak` に残ります）。

**毎回人が判定**: `suite.json` に `"human_review": true` を書くか、`evolve` / `evaluate` に `--human-review on` を付けます。
採点が必要になるたびにレビューシート（`evolve` は `<ワークスペース>/reviews/*.csv`、`evaluate` は `<--out>/review.csv`）が作られて
処理が止まるので、`verdict` に `OK` / `NG` / 0〜1 の数値を入れて同じコマンドを再実行します。
シートの `answer_key` 列は判定した出力の目印なので編集・削除しないでください（出力が変わったタスクは判定し直しになります）。
`=` や `-` で始まる文字は Excel が数式として扱わないよう先頭に `'` が付いていますが、読み込み時に自動で外れます。
`--human-review off` で自動採点に切り替えられます（その採点方法に必要な正解がタスクに入っていること）。

## コマンド

| コマンド | 用途 |
|---|---|
| `check` | Python・CLI・スイート（`--suite`）の確認。`--no-call` でモデルを呼ばない |
| `evolve SUITE --workspace DIR` | スキルを育てる。再実行で再開、`--iterations` を増やせば延長 |
| `evaluate SUITE --out DIR` と `--workspace DIR` / `--skills DIR` / `--no-skills` のどれか | 指定したスキルで採点し `report.json` を出力（`--split` 既定 test） |
| `compare A.json B.json` | 2 つの評価結果の差を検定 |
| `review` / `review-apply` | ハイブリッド方式のレビュー |
| `status DIR` | 反復ごとの採否と検証スコアを表示 |
| `export DIR --dest DIR` | スキルを Claude Code / Codex のスキルフォルダへ書き出し |

モデルを使うコマンド（`check`・`evolve`・`evaluate`・`review`）では `--backend claude|codex`、`--model`、
`--backend-config`（CLI の引数を変える。[docs/CONFIGURATION.md](docs/CONFIGURATION.md)）が使えます。
`evolve`・`evaluate`・`review` には `--workers`（並列数、既定 4）、`evolve`・`evaluate` には `--judge-model`（llm_judge の採点モデル）
と `--human-review on|off` もあります。全オプションは `python -m wikiskill <コマンド> --help` で確認できます。

| 終了コード | 意味 | 対処 |
|---|---|---|
| 0 | 成功 | |
| 1 | 実行時のエラー（CLI やモデルの失敗、同じワークスペースで実行中など） | 原因を解消して同じコマンドを再実行（完了分は再利用される） |
| 2 | 入力・設定のエラー（スイートやオプションの誤り） | メッセージに従って修正 |
| 3 | 人のレビュー待ち | レビューシートを記入して再実行 |

## 出力（ワークスペース）

```
workspaces/sales/
  skills/<名前>/SKILL.md, PURPOSE.md   採用中のスキル（PURPOSE.md は根拠となったパターンと採用履歴）
  wiki/index.md                        パターンの目次（問題・原因・対策を 1 行ずつ）
  wiki/patterns/*.md                   失敗/成功パターンの詳細
  wiki/logs.md                         反復ごとの発見の記録
  wiki/skill-impact.md                 すべての提案の差分・検証スコア・採否
  raw/iter_NNN/traces/*.md             train の実行記録（書き換えない）
  runs/  reviews/  state.json  run.log 再開用データ、レビューシート、進捗、ログ
```

`python -m wikiskill status workspaces/sales` で進捗を確認できます。
業務データを含むため、保存先の扱いは [docs/CONFIGURATION.md](docs/CONFIGURATION.md) の 4 章を確認してください。

## コストの目安

最初に val 件数分、その後 1 反復ごとに train 件数＋val 件数分のエージェント実行があります。
加えて Wiki メンテナが 1 回（応答が壊れていた場合は最大 3 回）、スキル提案者が最大 20 回のテキスト生成を行い、
`llm_judge` 採点なら採点 1 件につき 1 回の呼び出しが増えます。まず少数のタスクと `--iterations 2` で試してください。

## テスト

```bash
python -m unittest discover -s tests -t .
```

モデルを呼ばずに（偽の CLI で）全機能を確認します。GitHub Actions で Windows / macOS / Linux × Python 3.9 / 3.13 を実行しています。

## ドキュメント

- [docs/SUITE_FORMAT.md](docs/SUITE_FORMAT.md) — 自分の業務のタスクと採点方法の書き方
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — CLI の設定、社内環境での注意点
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 仕組み、コード構成、論文との対応と差分
