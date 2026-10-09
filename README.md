# WikiSkill

AI エージェント（Claude Code / Codex）に業務タスクを繰り返し解かせ、その実行記録から
**「スキル（SKILL.md）」を自動で育てる**ツールです。論文
[WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution (arXiv:2608.27454)](https://arxiv.org/abs/2608.27454)
の手法を実装しています（公式実装は公開されていないため、論文本文・Algorithm 1・付録のプロンプトに基づく独自実装です）。

- **Python 3.9 以上の標準ライブラリだけ**で動きます（`pip install` 不要）。Windows / macOS 対応。
- モデル呼び出しは手元の **`claude` / `codex` コマンド経由のみ**。WikiSkill 自身はネットワークに一切接続しません
  （CLI が Bedrock 等で認証済みなら、そのまま使えます）。
- できあがったスキルは Claude Code（`.claude/skills/`）や Codex（`.agents/skills/`）にそのまま入れて使えます。

## しくみ（概要）

```
              ┌──────────── 1 反復 ─────────────────────────────────────────────┐
 tasks.jsonl  │ ① 推論エージェント   train タスクを現在のスキルで解く → raw/ に実行記録     │
 (train/val/  │ ② Wiki メンテナ      失敗・成功の記録を分析 → wiki/ のパターン集を更新    │
  test)       │ ③ スキル提案者       wiki と記録を読み、スキルを1つ作成/修正する案を出す  │
              │ ④ 検証ゲート         val で採点し、スコアが上がった時だけ採用（下がれば戻す）│
              └──────────────────────────────────────────────────────────────────┘
   wiki/ は採用・不採用にかかわらず蓄積し続ける（＝失敗した試みも次の反復で活かされる）
```

詳しい構造と論文との対応は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。

## 必要なもの

| もの | 確認方法 |
|---|---|
| Python 3.9 以上 | `python --version`（Windows は `py --version`） |
| `claude` または `codex` コマンド（ログイン/認証済み） | `claude -p "hello"` または `codex exec "hello"` が返答すること |

## 入手方法

- **Git**: `git clone https://github.com/R1ck29/wikiskill.git` して `cd wikiskill`
- **ZIP**: GitHub の「Code → Download ZIP」で取得し、展開したフォルダ（`wikiskill-main`）に移動

以降のコマンドはすべて**このフォルダの中で**実行します。Windows では `python` を `py` に読み替えてください。

## クイックスタート

```bash
python -m wikiskill check --model sonnet
```

`complete(): 'OK'` と `run_agent(): ok=True answer='42'` が出れば準備完了です（`--backend codex` で Codex も確認できます）。

```bash
python -m wikiskill evolve examples/sales-analysis --workspace workspaces/sales --model haiku --iterations 4
```

付属のサンプル（売上 CSV の集計。社内ルール「金額は税抜で報告」をエージェントが経験から学ぶ必要がある）でスキルを育てます。
途中で止めても、同じコマンドを再実行すれば続きから再開します。

```bash
python -m wikiskill evaluate examples/sales-analysis --no-skills --out eval/base --model haiku
```

```bash
python -m wikiskill evaluate examples/sales-analysis --workspace workspaces/sales --out eval/skills --model haiku
```

```bash
python -m wikiskill compare eval/base/report.json eval/skills/report.json
```

test（未使用のタスク）でスキルなし／ありを比べ、差が偶然でないかをブートストラップ検定で確認します。

```bash
python -m wikiskill export workspaces/sales --dest .claude/skills
```

育ったスキルを Claude Code のプロジェクトスキルとして書き出します（Codex なら `--dest .agents/skills`）。

## 自分の業務で使う

1. 同じ種類の業務（例:「月次売上の集計」「契約書の英訳」）を 1 つの**スイート**にまとめます。
   train / val / test に分け、論文と同程度（train 15〜80、val 10〜40 件）あると安定します。
2. 正解の決め方を選びます。

| 業務の性質 | 採点方法 | 例 |
|---|---|---|
| 正解が数値・文字列で決まる | `number` / `exact` / `contains` / `regex` | データ抽出、集計、Excel の数値分析 |
| 出力ファイルを機械的に確認できる | `script`（Python 関数を自作） | Excel 編集、CSV 加工、モデル精度の閾値確認 |
| 正解が 1 つに決まらない | `llm_judge`（採点基準を文章で書く） | 翻訳、調査レポート、パワポの構成 |
| 正解をまだ用意していない | 人間レビュー（下記） | どの業務でも |

形式の詳細は [docs/SUITE_FORMAT.md](docs/SUITE_FORMAT.md) を参照してください。

### 人間が OK / 要修正 で判定する（2 つのモード）

**ハイブリッド（推奨）**: 最初に 1 回だけ人が確認し、その結果を正解データにして以降は自動採点します。

```bash
python -m wikiskill review my-suite --out review
```

`review/review_sheet.csv` を Excel で開き、`verdict` に `OK` / `NG`、NG の行は `corrected_answer` に正しい答え、
必要なら `rubric_note` に「良い答えの条件」を書いて保存します。

```bash
python -m wikiskill review-apply my-suite review/review_sheet.csv
```

tasks.jsonl に正解・採点基準が書き込まれます（元ファイルは `tasks.jsonl.bak` に保存）。

**毎回人間が判定**: `suite.json` の `"human_review": true`、またはコマンドに `--human-review on` を付けます。
採点が必要になるたびに `workspaces/<名前>/reviews/*.csv` が作られて処理が止まる（終了コード 3）ので、
`verdict` を記入して同じコマンドを再実行します。`--human-review off` でいつでも自動採点に戻せます。

## コマンド一覧

| コマンド | 用途 |
|---|---|
| `check` | Python・CLI・（`--suite` で）スイートの確認。`--no-call` でモデルを呼ばない |
| `evolve SUITE --workspace DIR` | スキルを育てる（再実行で再開。`--iterations` を増やせば延長） |
| `evaluate SUITE --out DIR (--workspace DIR \| --skills DIR \| --no-skills)` | 指定スキルで採点し `report.json` を出力（別モデルへのスキル転用評価にも使用） |
| `compare A.json B.json` | 2 つの評価の差を対応ありブートストラップ（1,000 回）で検定 |
| `review` / `review-apply` | ハイブリッド方式の人間レビュー |
| `status DIR` | 反復ごとの採用/不採用と検証スコアを表示 |
| `export DIR --dest DIR` | スキルを Claude Code / Codex のスキルフォルダへ書き出し |

共通オプション: `--backend claude|codex`、`--model`、`--judge-model`（llm_judge 用）、`--backend-config`（CLI の引数変更、
[docs/CONFIGURATION.md](docs/CONFIGURATION.md)）、`--workers`（並列数、既定 4）。
各コマンドの全オプションは `python -m wikiskill <コマンド> --help` で確認できます。

終了コード: `0` 成功 / `1` 実行エラー（CLI 障害などで中断。再実行で失敗分だけやり直し）/ `2` 設定エラー / `3` 人間レビュー待ち

## 出力（ワークスペース）

```
workspaces/sales/
  skills/<名前>/SKILL.md, PURPOSE.md   いま有効なスキル（PURPOSE.md は根拠となった wiki パターン）
  wiki/index.md                        パターンの目次（問題・原因・対策を 1 行で）
  wiki/patterns/*.md                   失敗/成功パターンの詳細
  wiki/logs.md                         反復ごとの発見の記録
  wiki/skill-impact.md                 すべての提案の差分・検証スコア・採否
  raw/iter_NNN/traces/*.md             train の実行記録（書き換え不可）
  runs/ reviews/ state.json run.log    再開用キャッシュ、レビューシート、進捗、ログ
```

ワークスペースには業務データを含む実行記録が残ります。共有・コミットしないでください（`.gitignore` 済み）。

## コストの目安

1 反復あたり、エージェント実行が（train 件数 + val 件数）回、テキスト生成が 1 回（Wiki メンテナ）+ 最大 20 回（スキル提案者）、
`llm_judge` の場合は採点が（train + val）回です。最初にベースラインとして val 件数分のエージェント実行があります。
まず `--iterations 2` と少数のタスクで試すことをおすすめします。

## テスト

```bash
python -m unittest discover -s tests -t .
```

モデルを呼ばない（偽の CLI を使う）テストです。GitHub Actions で Windows / macOS / Linux × Python 3.9 / 3.13 を実行しています。

## ドキュメント

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 仕組み、モジュール構成、論文との対応と差分
- [docs/SUITE_FORMAT.md](docs/SUITE_FORMAT.md) — suite.json / tasks.jsonl / 採点方法の書き方
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — CLI バックエンドの設定、社内環境での注意点
