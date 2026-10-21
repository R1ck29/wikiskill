# スイートの書き方

スイートは「同じ種類の業務」のタスクをまとめたフォルダです。スキルはスイートごとに育ちます。
異なる種類の業務を 1 つのスイートに混ぜないでください（スキルは「その業務の手順」として育つため）。

```
my-suite/
  suite.json      設定
  tasks.jsonl     タスク（1 行 1 件の JSON）
  files/          タスクの入力ファイル（任意）
  scorer.py       採点スクリプト（script 採点のときだけ）
```

完成例: `examples/sales-analysis`（数値で採点）、`examples/translation-ja-en`（llm_judge で採点）。
作ったら `python -m wikiskill check --suite my-suite --no-call` で形式を確認できます。

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
| `system_prompt` | ○ | 推論エージェントへの指示。**`{skill_section}` を必ず含めます**（ここにスキルが入ります）。最終回答を `<answer>...</answer>` で囲むよう指示してください |
| `system_prompt_file` | | `system_prompt` の代わりにファイルで指定（suite.json からの相対パス） |
| `tasks` | ○ | タスクファイルのパス（suite.json からの相対パス） |
| `files_dir` | | 入力ファイルのフォルダ（既定 `files`） |
| `scorer` | ○ | 採点方法（下記） |
| `pass_threshold` | | このスコア以上を「成功」とする（既定 1.0）。Wiki メンテナに渡す成功/失敗の記録の選び方と、スキル提案者が見る PASS/FAIL の表示に使われます |
| `human_review` | | `true` で毎回人が採点（既定 `false`）。コマンドの `--human-review on/off` が優先 |

## tasks.jsonl

```json
{"id": "sales-001", "split": "train", "prompt": "東京店の1月の純売上は？", "answer": "4090", "files": ["2026-01/sales.csv"]}
```

| キー | 必須 | 意味 |
|---|---|---|
| `id` | ○ | 英数字で始まり、英数字と `. _ -` だけの 100 字以内。ファイル名に使うため、大文字小文字だけが違う重複や `CON`・`NUL` などは使えません |
| `split` | ○ | `train`（学習用）/ `val`（採否の判定用）/ `test`（最終評価用）。`evolve` には train と val が各 1 件以上必要 |
| `prompt` | ○ | タスク文 |
| `answer` | 採点方法による | 正解。複数の正解はリスト（`["東京", "Tokyo"]`） |
| `files` | | `files_dir` からの相対パス（`..` は不可）。エージェントの作業フォルダに同じ相対パスでコピーされます |
| `rubric` | | このタスクだけの採点基準（llm_judge 用。共通の `rubric` に追記されます） |

**件数の目安**: 論文では train 16〜80、val 10〜40、test 85〜280 件でした。val が少ないと採否の判定がぶれます。

**途中での変更**: 正解・採点基準・タスク文は途中で直せます（タスク文やファイルを変えたタスクは次回に再実行されます）。
train / val のタスクを**追加・削除**した場合、`evolve` は点数を比べられなくなるためエラーで止まります（新しい `--workspace` で始めてください）。
`improve` は同じワークスペースで続け、今のスキルで val を採点し直した点数を新しい基準にします（反復の途中で変えた場合はエラー）。

## 採点方法（`scorer.type`）

採点の対象は、エージェントの最後のメッセージ中の最後の `<answer>...</answer>` の中身です（無ければメッセージ全体）。

| type | 判定 | 追加設定 |
|---|---|---|
| `exact` | 完全一致（全角→半角、大文字小文字、空白を正規化して比較） | |
| `contains` | 正解が回答に含まれる（同じ正規化） | |
| `regex` | `answer` を正規表現として回答を検索（大文字小文字は区別しない。正規化はしない） | |
| `number` | 回答中の最初の数値（全角→半角、`,` は無視）が正解に近い | `abs_tol`・`rel_tol`（既定ともに 1e-6） |
| `script` | 自作の Python 関数で採点 | `path`: スクリプトのパス（suite.json からの相対パス） |
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

戻り値は 0〜1 の数値、または `(数値, メモ)`。例外が起きたタスクは 0 点になり、例外の内容がメモに残ります。
Excel ファイルの中身を確かめたい場合は、社内で使えるライブラリ（openpyxl など）をこのスクリプトの中で使ってください
（WikiSkill 本体は標準ライブラリだけで動きます）。

### llm_judge

採点用モデルには、タスク文・参照解答（`answer`）・採点基準（共通 `rubric` ＋ タスクの `rubric`）・エージェントの最後のメッセージ・
作業フォルダのファイル名一覧が渡され、`{"score": 0〜1, "reason": "..."}` を返させます。`--judge-model` で採点モデルを分けられます。

採点モデルは**ファイルの中身も実行記録も見られません**。用語集など採点に必要な参照情報は `rubric` に直接書いてください
（付属の翻訳例を参照。書かないと採点が推測になり、検証ゲートの判断がぶれます）。成果物がファイルで中身の確認が必要なら、
`script` 採点を選び、スクリプトの中で確認してください。

## 業務別の選び方

| 業務 | 採点方法 |
|---|---|
| データ抽出・集計・Excel の数値分析 | `number`。入力ファイルを `files` で渡し、回答は数値 1 つにする |
| キャンペーン効果測定 | 正解値（効果量など）を用意して `number`、許容誤差は `abs_tol` |
| AI モデル作成 | `script` で出力（予測値ファイルなど）を読み、精度が閾値以上なら 1 |
| Excel 編集・CSV 加工 | `script` で出力ファイルを検査 |
| 翻訳・調査・パワポの構成 | `llm_judge`＋具体的な採点基準 |
| 正解がまだ無い | [README](../README.md) の「正解を人が決める」の方法で 1 回人が確認して正解化 |

`llm_judge` と人の採点は論文では評価されていない使い方です。採点のぶれは検証ゲートの判断に直接影響するため、
採点基準を具体的に書き、val を多めに用意してください。
