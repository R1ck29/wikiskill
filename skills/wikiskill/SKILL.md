---
name: wikiskill
description: 業務の例（入力と正解）からエージェント用スキルを自動で育てる WikiSkill を、利用者の代わりに実行する。「この業務を覚えさせて」「スキルを育てて」「WikiSkill」「育成の続き・進み具合」「育ったスキルを使えるようにして」と言われたとき、および育ったスキルの結果を利用者が評価したとき（フィードバックの記録）に使う。
argument-hint: "[やりたいこと（例: 月次売上の集計を覚えさせて）]"
allowed-tools: Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py" *) Bash(python "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py" *) Bash(py "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py" *) Edit(~/wikiskill-work/**)
---

# WikiSkill を利用者の代わりに動かす

利用者はターミナルに不慣れな業務担当者です。コマンドはすべてあなたが実行し、利用者に頼むのは
**例のフォルダを教えてもらうことだけ**です（採点・書き出し先の選択・OK/NG 判定は頼まない）。説明は専門用語を避けた短い日本語で行います。

## 実行方法とトークン節約（必ず守る）

`WS` は `python3 "${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py"` を表します（パスは必ずこの形でダブルクォート。
Windows は `python3` を `py`、無ければ `python` に）。
`WS` は単独で実行し、`;` `&&` `echo` などを付け足さない（終了コードは結果に表示される。付け足すと許可の確認が出る）。

- 最初に `WS check --no-call`。`claude`（または `codex`）が `NOT FOUND` なら、社内の手順で CLI を入れるよう伝えて止まる。
- 処理はコマンドに任せる。`raw/` `runs/` `wiki/` や実行記録は読まない。エラー時だけ `run.log` の末尾 30 行を読む。
- `improve` は Bash の `run_in_background` で実行し、完了通知を待つ（sleep・ポーリングしない）。
- 利用者の例ファイルは件数と形式がわかる分だけ見る。

終了コード: 0 成功 / 1 実行エラー（原因を直して同じコマンドを再実行すると続きから再開）/
2 入力の誤り（メッセージに従って直す）/ 3 人の判定待ち（利用者が人の判定を望んだときだけ。下の「人の判定」へ）。

## 置き場所（固定）

業務ごとに `~/wikiskill-work/<業務名>/suite`（スイート）と `~/wikiskill-work/<業務名>/workspace`（育成記録）。
業務名は英小文字とハイフン。育ったスキルは常に `~/.claude/skills` に自動で書き出す（利用者に聞かない）。
以下 `S`・`W` はそのスイートとワークスペースのパス。

## A. 新しい業務を覚えさせる

1. 利用者に「例が入ったフォルダ」を 1 つ聞く（入力ファイルと、あれば正解）。
2. `WS init S --like sales-analysis --inputs <例のフォルダ>`（数値の答え。文章の業務は `--like translation-ja-en`）で
   `S` を作る（入力ファイルは `S/files/` にコピーされ、一覧が表示される）。`S/suite.json` を業務に合わせて直し、
   `S/tasks.jsonl` を 1 行 1 件で書く（書式は表示された完成例の tasks.jsonl と同じ。分割の目安: train 半分、
   val 4 分の 1、test 4 分の 1。各 1 件以上。少なければ test を減らす）。
   - `system_prompt` には `{skill_section}` と「最終回答を `<answer>...</answer>` で囲む」指示を残す。
   - 採点: 正解があり数値→`number`、決まった文言→`exact`/`contains`、出力ファイルの検査→`script`。
     **正解が無い・文章の業務は `llm_judge`** にし、例と利用者の説明から「良い結果の条件」を具体的に `rubric` に書く
     （用語・形式・必須項目など。利用者には確認しない）。
   - `WS check --suite S --no-call` が通るまで直す。
3. `WS improve S --workspace W --export ~/.claude/skills` をバックグラウンドで実行
   （1 反復で train＋val 件数分のエージェント実行がかかることを一言伝える）。
4. 終わったら「最初の点数 → 最後の点数」「使えるようになったスキル名」を 2〜3 行で伝え、
   「結果が良い・違うと言ってもらえれば、それを覚えて自動で改善します」と添える。

## B. フィードバックの記録（実際の利用から改善）

育ったスキル（末尾に「Feedback for automatic improvement」がある）で作業した結果を利用者が評価したら、その会話の中で記録する:

`WS feedback S --verdict good|bad --prompt '<利用者の依頼>' --output '<出した結果>'`
（違うと言われ正しい内容がわかれば `--correct '<正しい結果>'`、理由は `--note '<理由>'`、入力ファイルは `--file <パス>` を足す）。

- 終了コード 2 で `--correct` が必要と出たら、正しい答えを一言だけ聞く（答えがなければ記録しない）。
- 出力に `ready` と出たら、利用者に断らずに A-3 と同じ `improve` をバックグラウンドで実行し、終わったら 1 行で伝える。
- 理由などを追加で聞かない。記録したことは「覚えました」と一言だけ伝える。
- 文中に `'` を含むときは、その引数をダブルクォートで囲む（`"` `$` `` ` `` は `\` を前に付ける）。

## C. 続き・進み具合

`WS improve S --workspace W --export ~/.claude/skills` を再実行すれば途中から再開し、何もなければ「nothing to do」で終わる。
進み具合だけなら `WS status W/gen-NNN`（一番大きい番号）。スキルあり／なしの厳密な比較（`evaluate`・`compare`）は利用者が求めたときだけ。

## 人の判定（利用者が望んだときだけ）

`improve` に `--human-review on` を付ける。終了コード 3 で止まったら、表示された CSV のパスを伝え、
「Excel で開き、`verdict` 列に OK か NG（または 0〜1 の点数）を入れて保存し、終わったら『できた』と送ってください。
NG の行は `corrected_answer` に正しい答えを、`answer_key` 列は触らないでください」と頼み、返事が来たら同じコマンドを再実行する。

## 参照（迷ったときだけ読む）

`docs/` と `examples/` は `${CLAUDE_SKILL_DIR}` の中、無ければ `${CLAUDE_SKILL_DIR}/../..` にあります。
書式と採点方法は `docs/SUITE_FORMAT.md`、社内ゲートウェイなど CLI の設定は `docs/CONFIGURATION.md`。
