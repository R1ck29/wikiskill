# アーキテクチャ

WikiSkill が何をどの順で行い、どのファイルが何を担当するかを説明します。
論文（arXiv:2608.27454）の対応箇所も併記します。使い方は [README](../README.md)、CLI の設定は [CONFIGURATION.md](CONFIGURATION.md) を参照してください。

## 1. 3 つの層

エージェントの経験を 3 つの層に分けて保存します（論文 §3.1）。

| 層 | 場所 | 中身 | 性質 |
|---|---|---|---|
| Raw | `raw/` | train タスクの実行記録（推論・ツール呼び出し・出力・最終回答・採点結果） | 一度書いたら変更しない |
| Wiki | `wiki/` | 実行記録から抽出した失敗/成功パターン、反復ログ、提案の採否履歴 | 毎回追記・更新されるが、リセットも巻き戻しもしない |
| Skill | `skills/` | エージェントに渡す手順書（SKILL.md）とその根拠（PURPOSE.md） | 検証で改善したときだけ更新。悪化なら元に戻す |

スキルは試行錯誤で行ったり来たりしますが、wiki は「何を試して、なぜダメだったか」まで残り続けます。
論文の比較実験（Table 3, Gemini-3.5-Flash）では、スキル提案者に wiki を読ませると平均正答率が 48.7% から 63.7% に上がりました。

## 2. 1 反復の流れ（論文 Algorithm 1）

```
開始時: val をスキルなしで解き、そのスコアを R_best とする（ベースライン）
反復 k = 1..K（R_best = 1.0 になったら終了）
  rollout   推論エージェントが train を現在のスキルで解く → 採点 → raw/iter_k/traces/<id>.md
  maintain  Wiki メンテナが失敗最大 5 件・成功最大 3 件（各 15,000 字まで）の記録を分析し、
            wiki/patterns/ の作成・追記、index.md の更新、logs.md への追記を行う
  propose   スキル提案者が wiki と記録を読み、提案を 1 つ出す（新規作成 / 既存スキル 1 つの部分修正 / 何もしない）
  validate  提案を適用した候補スキルで val を解いて採点
  commit    R(val) > R_best のときだけ採用（同点は不採用）。不採用ならスキルだけ元に戻す。
            採否・差分・スコアを wiki/skill-impact.md に記録（プログラムが書く）
```

各ステージは結果をディスクに保存してから次へ進み、現在位置を `state.json` に記録します。
途中で止まっても同じ `evolve` コマンドで続きから再開でき、人のレビュー待ちもこの仕組みで実現しています。

### 各エージェントに見せる情報

| エージェント | 見えるもの | 見えないもの |
|---|---|---|
| 推論エージェント（CLI のエージェント実行） | タスク文、入力ファイル、システムプロンプトに全文埋め込みのスキル | wiki、他タスクの記録（作業はワークスペース外の一時フォルダ） |
| Wiki メンテナ（テキスト生成 1 回。応答が壊れていれば最大 3 回） | wiki 全体、採用中のスキル、抽出した train の記録 | val・test の記録 |
| スキル提案者（ReAct、最大 20 ターン） | 最初に wiki/index.md・skill-impact.md・train 全件の結果一覧。以降は `read_file` で `wiki/`・`skills/`・`traces/<id>` を必要に応じて読む | val・test の記録、ワークスペース外 |

推論エージェントに wiki を見せないのは、論文の比較実験（Table 3）で見せると最終性能が下がった（63.7% → 60.9%）ためです。

## 3. コード構成

```
wikiskill/
  cli.py          コマンド定義と終了コード
  evolve.py       Algorithm 1 の反復ループとステージ管理（再開・人のレビュー待ち・ワークスペースのロック）
  rollout.py      推論エージェントの実行（一時フォルダ・並列実行・結果キャッシュ・実行記録の整形）
  maintainer.py   Wiki メンテナ（記録の抽出、プロンプト作成、JSON 編集の適用）
  proposer.py     スキル提案者（ReAct ループ、読み取り専用サンドボックス、提案の検証）
  scoring.py      採点（exact/contains/regex/number/script/llm_judge）とレビューシート
  review.py       ハイブリッド方式（1 回だけ人が確認 → 正解データ化）
  improve.py      実際の利用からの改善（feedback の記録、gen-NNN 世代の準備と引き継ぎ、フッター付きの書き出し）
  evaluate.py     任意のスキルでの評価と、対応ありブートストラップ検定
  skills.py       SKILL.md の検証（Agent Skills 仕様）、プロンプトへの埋め込み、提案の適用
  patching.py     append / replace / insert_after の部分編集と差分（wiki とスキルで共用）
  workspace.py    3 層のフォルダ構成、raw の書き込み一回限り保証、state.json、ロック
  config.py       suite.json / tasks.jsonl の読み込みと検証
  backends.py     claude / codex CLI の呼び出し（タイムアウト・中断時のプロセス停止、再試行）
  util.py         UTF-8 入出力、LLM 出力からの JSON 抽出、切り詰め、CSV のエスケープ
  prompts/        Wiki メンテナとスキル提案者のシステムプロンプト（論文付録 E.2 / E.3）
```

プラグインとしての配布物（README の「Claude のプラグインとして使う」）:

```
.claude-plugin/plugin.json, marketplace.json   リポジトリ全体を 1 つのプラグイン兼マーケットプレイスにする
skills/wikiskill/SKILL.md                      Claude が CLI を代わりに実行するための手順書（呼び出し時だけ読み込まれる）
skills/wikiskill/scripts/run_wikiskill.py      起動スクリプト。スキルフォルダ内 → 親フォルダの順に wikiskill パッケージを探し、
                                               Python が 3.9 未満なら PATH 上の新しい python3.X で実行し直す
tools/integrate.py                             既存プラグインへ、パッケージ・docs・examples を同梱した自己完結のスキルフォルダをコピー
```

依存の向きは `cli → evolve/evaluate/review → rollout/maintainer/proposer → scoring/skills/backends → config/patching/workspace/util` の一方向です。

拡張するときに触る場所:

- **採点方法を追加**: `config.py` の `SCORER_TYPES`（正解必須なら `ANSWER_REQUIRED` も）と、`scoring.py` の `Scorer.auto`
- **CLI を追加**: `backends.py` で `CLIBackend` を継承したクラスを作って `DEFAULTS` と `BACKENDS` に登録し、`cli.py` の `--backend` の選択肢に加える

## 4. 論文との対応と差分

論文どおりに実装した部分:

| 論文 | 実装 |
|---|---|
| 3 層構成（§3.1） | `workspace.py` |
| スキルの全文埋め込み（§3.2.1） | `skills.render_skill_section` → システムプロンプトの `{skill_section}` |
| メンテナの抽出: 失敗 ≤5・成功 ≤3・各 15,000 字（付録 C） | `maintainer.sample_traces`, `maintainer.TRACE_CHARS` |
| メンテナのプロンプトと JSON 編集形式（付録 E.2） | `prompts/wiki_maintainer.md`, `maintainer.apply_output` |
| 提案者の ReAct・`read_file`/`finish`・初期入力（§3.2.3, 付録 E.3） | `proposer.py`, `prompts/skill_proposer.md` |
| `traces/<id>` を raw の記録に対応付け（付録 E.3 末尾） | `proposer.ReadSandbox` |
| 厳密な改善でのみ採用・スキルだけ巻き戻し・wiki は保持（式 4, Algorithm 1） | `evolve.py` の `Evolution._validate`, `Evolution._commit` |
| R_best = 1.0 で早期終了（Algorithm 1 の 4 行目） | `evolve.py` の `Evolution._run` |
| skill-impact.md に提案・差分・スコア・採否を追記（§3.2.4） | `evolve._record_skill_impact` |
| 対応ありブートストラップ 1,000 回（付録 C） | `evaluate.paired_bootstrap` |

論文に明記がなく実装上決めたこと、または意図的な差分:

1. **反復回数 K の既定値 8**: 論文に K の明記はありません。付録 Table 5 が反復 0〜7 を集計しているため 8 としました（`--iterations` で変更可。反復番号は 1 から数えます）。
2. **ログのファイル名 `logs.md`**: 本文と Figure 3 は `logs.md`、付録 E.2 のプロンプトは `log.md` と食い違うため、本文に合わせてプロンプトの 1 行を修正しました。
3. **スキル名は英小文字・数字・単一ハイフン**: 付録 E.3 は snake_case ですが、Claude Code / Codex でそのまま使えるよう
   Agent Skills 仕様（64 字以内、フォルダ名と一致）に合わせ、プロンプトの 1 行を修正しました。
4. **プロンプト末尾の「harness note」**: JSON での応答形式、パターンファイル名の規則、SKILL.md の書式を追記しています。それ以外のプロンプト本文は付録のままです。
5. **メンテナに採用中のスキルも渡す**: 付録 E.2 の指示「スキルに従ったか確認せよ」を実行できるようにするためです。
6. **「実行記録を 4 件以上読む」規則をプログラムでも確認**: 付録 E.3 の規則 4。満たさない変更提案や、
   適用できない提案（名前の規則違反・置換対象が見つからない）は差し戻して提案者に直させます。何もしない提案（`no_action`）はそのまま受け付けます。
7. **`no_action` は検証を省略**: 同じスキルで val を解き直しても「厳密な改善」にならず必ず不採用になるため、結果は同じでコストだけ省きます。
8. **置換対象が複数箇所に一致する編集は失敗扱い**: 意図しない箇所の書き換えを防ぐためです。
9. **長い実行記録は先頭と末尾を残して切り詰め**: 最終回答が末尾にあるためです。
10. **採用結果を PURPOSE.md の履歴に 1 行追記**し、skill-impact.md には提案の PURPOSE.md も記録します。
11. **CLI の障害は採点しない**: 障害を「不正解」として wiki や採否判定に混ぜないためです（[CONFIGURATION.md](CONFIGURATION.md) 4 章）。

論文にない追加機能: 人による採点（ハイブリッド / 毎回）、`script`・`llm_judge` などの採点方法、途中再開、
`evaluate` / `compare` / `export` コマンド。論文が挙げる限界（スキル検索をしない全文埋め込み、wiki の自動整理がない、
毎回の改善を必須とする厳しい採否基準など）はこの実装にもそのまま当てはまります。
