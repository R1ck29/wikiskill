# スイートの書き方

スイートは「同じ種類の業務」をまとめたフォルダです。スキルはスイートごとに育ちます。

```
my-suite/
  suite.json      設定
  tasks.jsonl     タスク（1 行 1 件）
  files/          タスクの入力ファイル（任意）
  scorer.py       採点スクリプト（script 採点のときだけ）
```

作ったら `python -m wikiskill check --suite my-suite --no-call` で形式を確認できます。
完成例は `examples/sales-analysis`（数値採点）と `examples/translation-ja-en`（llm_judge 採点）です。

## suite.json

```json
{
  "name": "monthly-sales",
  "task_description": "Japanese monthly sales questions answered from CSV exports",
  "system_prompt": "You are a data analyst.\n{skill_section}\nGive the final number inside <answer>...</answer>.",
  "tasks": "tasks.jsonl",
  "files_dir": "files",
  "scorer": {"type": "number", "abs_tol": 0.5},
  "pass_threshold": 1.0,
  "human_review": false
}
```

| キー | 必須 | 意味 |
|---|---|---|
| `name` | ○ | スイート名。ワークスペースはこの名前と結び付きます |
| `task_description` | ○ | スキル提案者に伝える「どんな業務か」の一文（英語推奨） |
| `system_prompt` | ○ | 推論エージェントへの指示。**`{skill_section}` を必ず含める**（ここにスキルが入ります）。最終回答を `<answer>...</answer>` で囲むよう指示してください |
| `system_prompt_file` | | `system_prompt` の代わりにファイルで指定（suite.json からの相対パス） |
| `tasks` | ○ | タスクファイルのパス |
| `files_dir` | | 入力ファイルのフォルダ（既定 `files`） |
| `scorer` | ○ | 採点方法（下記） |
| `pass_threshold` | | このスコア以上を「成功」とみなす（既定 1.0）。成功/失敗は Wiki メンテナへ渡す記録の選び方に使われます |
| `human_review` | | `true` で毎回人間が採点（既定 `false`）。`--human-review on/off` で上書き可 |

## tasks.jsonl

```json
{"id": "sales-001", "split": "train", "prompt": "東京店の1月の純売上は？", "answer": "4090", "files": ["2026-01/sales.csv"]}
```

| キー | 必須 | 意味 |
|---|---|---|
| `id` | ○ | 英数字と `. _ -`（100 字以内）。ファイル名に使うため大文字小文字違いの重複や `CON` などは不可 |
| `split` | ○ | `train`（学習）/ `val`（採否の判定）/ `test`（最終評価のみ）。`evolve` には train と val が各 1 件以上必要 |
| `prompt` | ○ | タスク文 |
| `answer` | 採点方法による | 正解。複数の正解はリスト `["東京", "Tokyo"]` で指定 |
| `files` | | `files_dir` からの相対パス。エージェントの作業フォルダに同じ相対パスでコピーされます |
| `rubric` | | このタスク専用の採点基準（llm_judge 用） |

分け方の目安: 論文では train 16〜80、val 10〜40、test 85〜280 件でした。val が少ないと採否の判定がぶれます。

## 採点方法（`scorer.type`）

回答は、エージェントの最終メッセージ中の最後の `<answer>...</answer>`（無ければ全文）です。比較前に全角→半角、
大文字小文字、空白の正規化を行います。

| type | 判定 | 追加設定 |
|---|---|---|
| `exact` | 正規化後に完全一致 | |
| `contains` | 正解が回答に含まれる | |
| `regex` | `answer` を正規表現として回答を検索（大文字小文字無視） | |
| `number` | 回答中の最初の数値（`,` は無視）が正解に近い | `abs_tol`（既定 1e-6）、`rel_tol`（既定 1e-6） |
| `script` | 自作の Python 関数で採点 | `path`: スクリプトのパス |
| `llm_judge` | モデルが採点基準に照らして 0〜1 で採点 | `rubric`: 全タスク共通の採点基準 |

### script

```python
# scorer.py
from pathlib import Path

def score(task, prediction, workdir):
    """task: {"id","prompt","answer","rubric"}  prediction: <answer> の中身  workdir: エージェントの作業フォルダ"""
    out = Path(workdir) / "result.csv"
    if not out.exists():
        return 0.0, "result.csv がない"
    return (1.0 if out.read_text(encoding="utf-8").strip() == task["answer"] else 0.0), "result.csv を比較"
```

戻り値は 0〜1 の数値、または `(数値, メモ)`。例外が起きた場合は 0 点としてメモに記録されます。
Excel ファイルを確認したい場合は、社内で使えるライブラリ（openpyxl など）をこのスクリプト内で使ってください
（WikiSkill 本体は標準ライブラリのみで動きます）。

### llm_judge

`rubric`（共通）とタスクの `rubric`（個別）を、参照解答・エージェントの回答・作業フォルダのファイル名一覧と一緒に
採点用モデルへ渡し、`{"score": 0〜1, "reason": "..."}` を返させます。`--judge-model` で採点用モデルを分けられます。
採点モデルはファイルの中身も実行記録も見られません。用語集など**採点に必要な参照情報は `rubric` に直接書いてください**（付属の翻訳例を参照。書かないと採点が推測になり、検証ゲートの判断がぶれます）。成果物がファイルの場合は `script` との併用（スクリプト側で中身を確認）を検討してください。

## 業務別のヒント

| 業務 | 推奨設定 |
|---|---|
| データ抽出・集計・Excel の数値分析 | `number`。入力ファイルを `files` で渡し、回答は数値 1 つにする |
| キャンペーン効果測定 | 正解値（効果量など）を用意して `number`、許容誤差を `abs_tol` で指定 |
| AI モデル作成 | `script` で出力ファイル（予測値など）を読み、精度が閾値以上なら 1 |
| Excel 編集・CSV 加工 | `script` で出力ファイルを検査 |
| 翻訳・調査・パワポ構成 | `llm_judge`＋具体的な採点基準。用語集などは `files` で渡す |
| 正解がまだ無い | ハイブリッド方式で 1 回人が確認して正解化（README 参照） |

1 つのスイートに異なる種類の業務を混ぜないでください。スキルは「その業務での手順」として育つためです。
