---
name: wikiskill
description: 業務の例（入力と正解）からエージェント用スキルを自動で育てる WikiSkill を、利用者の代わりに実行する。「この業務を覚えさせて」「スキルを育てて」「WikiSkill」「育成の続き・進み具合」「育ったスキルを使えるようにして」と言われたときに使う。
argument-hint: "[やりたいこと（例: 月次売上の集計を覚えさせて）]"
allowed-tools: Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py" *) Bash(python "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py" *) Bash(py "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py" *)
---

# WikiSkill を利用者の代わりに動かす

利用者はターミナルに不慣れな業務担当者です。コマンドはすべてあなたが実行し、利用者には
**フォルダの指定と OK/NG の判定だけ**をお願いします。説明は専門用語を避けた短い日本語で行います。

## トークン節約のルール（必ず守る）

- 処理はコマンドに任せる。`raw/` `runs/` `wiki/` `run.log` や実行記録は**読まない**。進捗は `status` の出力だけで判断する。
- エラー時だけ `run.log` の末尾 30 行を読む（`tail -n 30`）。
- 時間のかかるコマンド（`evolve` `evaluate` `review`）は Bash の `run_in_background` で実行し、**完了通知を待つ**（sleep やポーリングをしない）。
- 例のファイルは件数と形式がわかる分だけ見る（全件を開かない）。下の参照文書は採点方法で迷ったときだけ読む。

## 実行方法

以下 `WS` は次のコマンドを表します（パスは必ずこの形でダブルクォートする）。

```
python3 "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py"
```

Windows では `python3` を `py` に、それも無ければ `python` に置き換えます（古い Python で起動しても、
新しい `python3.X` があれば自動でそちらが使われます）。最初に `WS check --no-call` を実行し、
Python 3.9 以上と `claude`（または `codex`）コマンドが見つかることを確認します。`NOT FOUND` なら、
社内の手順で Claude Code の CLI を入れる必要があると利用者に伝えて止まります。

終了コード: 0 成功 / 1 実行エラー（原因を直して同じコマンドを再実行すると続きから再開）/
2 入力の誤り（メッセージに従って直す）/ 3 人の判定待ち（下の「人の判定」へ）。

## 手順

作業フォルダは現在のプロジェクト直下の `wikiskill-work/<業務名>/`（`suite/` と `workspace/`）にまとめます。
業務名は英小文字とハイフン（例: `monthly-sales`）。

1. **何をしたいか確認**: 新しい業務を覚えさせる／続き・進み具合／育ったスキルを使えるようにする、のどれか。
   続きなら `WS status <workspace>` で答え、必要なら手順 4 を同じコマンドで再実行（自動で再開）。
2. **スイートを作る**（新しい業務のとき）: 利用者に「例が入ったフォルダ」を 1 つ聞く（入力ファイルと、わかれば正解）。
   - `suite/suite.json`、`suite/tasks.jsonl`、`suite/files/`（入力ファイルをコピー）を作る。書式は参照文書を参照。
   - `system_prompt` には `{skill_section}` と「最終回答を `<answer>...</answer>` で囲む」指示を必ず入れる。
   - 採点方法: 数値の答え→`number`、決まった文言→`exact`/`contains`、翻訳・文章→`llm_judge`（採点基準を具体的に `rubric` に書く）、
     出力ファイルの検査→`script`。迷ったら利用者に「答えは数字・決まった言葉・文章・ファイルのどれか」だけ聞く。
   - 分割の目安: train 半分、val 4 分の 1、test 4 分の 1（各 1 件以上。少ない場合は test を減らす）。
   - `WS check --suite <suite> --no-call` が通るまで直す。
3. **正解が無い場合**: `WS review <suite> --out <業務フォルダ>/review` を実行し、「人の判定」の手順で
   シートを記入してもらってから `WS review-apply <suite> <業務フォルダ>/review/review_sheet.csv`。
4. **育てる**: 費用の目安（1 反復で train＋val 件数分のエージェント実行）を一言伝えてから
   `WS evolve <suite> --workspace <workspace> --iterations 3` をバックグラウンドで実行。終わったら `WS status <workspace>` を実行する。
5. **報告**: 「最初の点数 → 最後の点数」「採用されたスキル名」「保存場所」を 3〜5 行で伝える。
   点数が上がらなかった場合は、例を増やす・採点基準を具体的にする、のどちらが効きそうかを一言添える。
6. **使えるようにする**: どこで使うかを聞き、`WS export <workspace> --dest <書き出し先>` を実行する。
   全プロジェクトで使う→`~/.claude/skills`、このプロジェクトだけ→`<プロジェクト>/.claude/skills`、Codex→`~/.agents/skills`。
   次の会話から使えることを伝える。

スキルあり／なしの厳密な比較（`evaluate` と `compare`）は費用がかかるため、利用者が求めたときだけ行います。

## 人の判定（終了コード 3）

出力に表示された CSV のパスを伝え、「Excel で開き、`verdict` 列に OK か NG（または 0〜1 の点数）を入れて保存し、
終わったら『できた』と送ってください。NG の行は `corrected_answer` に正しい答えを書いてください。
`answer_key` 列は触らないでください」と頼みます。返事が来たら同じコマンドを再実行します。

## 参照文書

スイートの書式と採点方法の詳細: `${CLAUDE_SKILL_DIR}/docs/SUITE_FORMAT.md`
（無ければ `${CLAUDE_SKILL_DIR}/../../docs/SUITE_FORMAT.md`）。完成例はその `docs/` と同じ階層の `examples/` にあります。
CLI の設定変更（社内ゲートウェイなど）は同じ場所の `docs/CONFIGURATION.md`。
