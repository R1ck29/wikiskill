# アーキテクチャ

この文書は、WikiSkill が何をどの順で行い、どのファイルがどの役割を持つかを説明します。
論文（arXiv:2608.27454）のどこに対応するかも併記します。

## 1. 基本の考え方

エージェントの経験を 3 つの層に分けて保存します（論文 §3.1）。

| 層 | 場所 | 中身 | 性質 |
|---|---|---|---|
| Raw | `raw/` | train タスクの実行記録（推論・ツール呼び出し・出力・最終回答・採点結果） | 一度書いたら変更しない |
| Wiki | `wiki/` | 実行記録から抽出した失敗/成功パターン、反復ログ、提案の採否履歴 | 決してリセットしない（積み上がる） |
| Skill | `skills/` | エージェントに渡す手順書（SKILL.md）とその根拠（PURPOSE.md） | 検証で改善した時だけ更新、悪化なら元に戻す |

スキルは試行錯誤で行ったり来たりしますが、wiki は「何を試して、なぜダメだったか」まで含めて残り続けます。
論文の比較実験（Table 3, Gemini-3.5-Flash）では、スキル提案者に wiki を読ませると平均正答率が 48.7% から 63.7% に上がりました。

## 2. 1 反復の流れ（論文 Algorithm 1）

```
開始時: val をスキルなしで解き、そのスコアを R_best とする（ベースライン）
反復 k = 1..K（R_best = 1.0 になったら終了）
  rollout   推論エージェントが train を現在のスキル S_{k-1} で解く
            → 採点 → raw/iter_k/traces/<id>.md に保存
  maintain  Wiki メンテナが、失敗最大 5 件・成功最大 3 件（各 15,000 字まで）を抽出して分析し、
            wiki/patterns/ の作成・追記、index.md の更新、logs.md への追記を行う（LLM 呼び出し 1 回）
  propose   スキル提案者が wiki と実行記録を読み、提案 P_k を 1 つ出す
            （新規作成 / 既存スキル 1 つの部分修正 / 何もしない）
  validate  候補スキル S'_k = Apply(S_{k-1}, P_k) で val を解いて採点
  commit    R(val) > R_best のときだけ採用（同点は不採用）。不採用ならスキルだけ元に戻す。
            採否・差分・スコアを wiki/skill-impact.md に記録（プログラムが書く）
```

各ステージは結果をディスクに保存してから次へ進み、現在位置を `state.json` に記録します。
そのため、途中で止まっても同じ `evolve` コマンドで続きから再開できます。人間レビュー待ちもこの仕組みで実現しています。

### 各エージェントに見せる情報

| エージェント | 見えるもの | 見えないもの |
|---|---|---|
| 推論エージェント（`claude` / `codex` のエージェント実行） | タスク文、入力ファイル、システムプロンプトに全文埋め込みのスキル | wiki、他タスクの記録（作業は workspace 外の一時フォルダで行う） |
| Wiki メンテナ（ツールなし 1 回呼び出し） | wiki 全体、有効なスキル、抽出した実行記録 | val の記録 |
| スキル提案者（ReAct、最大 20 ターン） | 最初に wiki/index.md・skill-impact.md・train 全件の結果一覧。以降は `read_file` で `wiki/`・`skills/`・`traces/<id>` を必要に応じて読む | val・test の記録、ワークスペース外 |

推論エージェントに wiki を見せないのは、論文の比較実験（Table 3）で見せると最終性能が下がったためです。

## 3. モジュール構成

```
wikiskill/
  cli.py          コマンド定義（check / evolve / evaluate / compare / review / review-apply / status / export）
  evolve.py       Algorithm 1 の反復ループとステージ管理（再開・人間レビュー待ちを含む）
  rollout.py      推論エージェントの実行（一時フォルダ・並列実行・結果キャッシュ・実行記録の整形）
  maintainer.py   Wiki メンテナ（抽出、プロンプト作成、JSON 編集の適用）
  proposer.py     スキル提案者（ReAct ループ、読み取り専用サンドボックス、提案の検証）
  scoring.py      採点（exact/contains/regex/number/script/llm_judge）と人間レビュー CSV
  review.py       ハイブリッド方式（1 回だけ人が確認 → 正解データ化）
  evaluate.py     任意のスキルでの評価と、対応ありブートストラップ検定
  skills.py       SKILL.md の検証（Agent Skills 仕様）、プロンプトへの埋め込み、提案の適用
  patching.py     append / replace / insert_after の部分編集と差分（wiki とスキルで共用）
  workspace.py    3 層のフォルダ構成、raw の書き込み一回限り保証、state.json
  config.py       suite.json / tasks.jsonl の読み込みと検証
  backends.py     claude / codex CLI の呼び出し（stdin 入力、タイムアウト時のプロセスツリー停止、再試行）
  util.py         UTF-8 入出力、LLM 出力からの JSON 抽出、切り詰め
  prompts/        Wiki メンテナとスキル提案者のシステムプロンプト（論文付録 E.2 / E.3）
```

依存の向きは `cli → evolve/evaluate/review → rollout/maintainer/proposer → scoring/skills/backends → config/patching/workspace/util` の一方向です。
新しい採点方法は `scoring.py`、新しい CLI は `backends.py` の `CLIBackend` を継承したクラスを `BACKENDS` に追加するだけで足せます。

## 4. CLI との連携

| 呼び出し | Claude Code | Codex |
|---|---|---|
| テキスト生成（メンテナ・提案者・採点） | `claude -p --output-format json --tools ""` ＋ `--system-prompt-file` | `codex exec --json --sandbox read-only`（システム文はプロンプトの先頭に付与） |
| エージェント実行（推論） | `claude -p --output-format stream-json --verbose --permission-mode bypassPermissions` ＋ `--append-system-prompt-file` | `codex exec --json --sandbox workspace-write -C <作業フォルダ>` |

- プロンプトは標準入力、長いシステムプロンプトは一時ファイルで渡し、コマンドラインには短いフラグだけを置きます
  （Windows のコマンド長制限と `.cmd` ラッパーでの文字化け・引用符崩れを避けるため）。
- Claude は `--safe-mode` で、利用者の CLAUDE.md・スキル・MCP・フックを読み込みません（実験の再現性とトークン節約のため）。
  認証とモデル選択はそのまま使われます。
- Web 検索は無効化しています（Claude: `--disallowedTools WebFetch,WebSearch`、Codex: `-c web_search=disabled`）。
- CLI 自体の障害（タイムアウト・API エラー・未インストール）は自動で再試行し、それでも失敗したタスクは
  **採点せずに**そのステージを中断します。障害を「不正解」として wiki や検証ゲートに混ぜないためです。
  再実行すると、完了済みのタスクはキャッシュを使い、失敗分だけやり直します。

既定の引数と変更方法は [CONFIGURATION.md](CONFIGURATION.md) を参照してください。

## 5. 論文との対応と差分

論文に忠実な部分:

| 論文 | 実装 |
|---|---|
| 3 層構成（§3.1） | `workspace.py`（raw 書き込み一回限り、wiki は不変・非リセット） |
| スキルの全文埋め込み（§3.2.1） | `skills.render_skill_section` → システムプロンプトの `{skill_section}` |
| メンテナの抽出: 失敗 ≤5・成功 ≤3・各 15,000 字（付録 C） | `maintainer.sample_traces`, `TRACE_CHARS` |
| メンテナのプロンプトと JSON 編集形式（付録 E.2） | `prompts/wiki_maintainer.md`, `maintainer.apply_output` |
| 提案者の ReAct・`read_file`/`finish`・初期入力（§3.2.3, 付録 E.3） | `proposer.py`, `prompts/skill_proposer.md` |
| `traces/<id>` を raw の記録に対応付け（付録 E.3 末尾） | `proposer.ReadSandbox` |
| 厳密な改善でのみ採用・スキルだけ巻き戻し・wiki は保持（式 4, Algorithm 1） | `evolve._validate`, `evolve._commit` |
| R_best=1.0 で早期終了（Algorithm 1 4 行目） | `evolve.Evolution.run` |
| skill-impact.md に提案・差分・スコア・採否を追記（§3.2.4） | `evolve._record_skill_impact` |
| 対応ありブートストラップ 1,000 回（付録 C） | `evaluate.paired_bootstrap` |

論文に明記がなく実装上決めたこと、または意図的な差分:

1. **反復回数 K の既定値 8**: 論文に K の明記はありません。付録 Table 5 が反復 0〜7 を集計しているため 8 としました（`--iterations` で変更可）。
2. **ログのファイル名 `logs.md`**: 本文と Figure 3 は `logs.md`、付録 E.2 のプロンプトは `log.md` と食い違うため、本文に合わせました。
3. **スキル名はハイフン区切り小文字**: 付録 E.3 は snake_case ですが、Claude Code / Codex でそのまま使えるよう
   Agent Skills 仕様（英小文字・数字・単一ハイフン、64 字以内、フォルダ名と一致）に合わせました。
4. **プロンプト末尾の「harness note」**: 付録のプロンプト本文はそのままに、JSON での応答形式と SKILL.md の書式だけを追記しています。
5. **メンテナに有効なスキルも渡す**: 付録 E.2 の指示「スキルに従ったか確認せよ」を実行できるようにするためです。
6. **「実行記録を 4 件以上読む」規則をプログラムでも確認**: 付録 E.3 の規則 4。満たさない `finish` や、
   適用できない提案（名前違反・置換対象なし）は差し戻して提案者に直させます。
7. **`no_action` は検証を省略**: 同じスキルで val を再評価しても「厳密な改善」にはならず必ず不採用になるため、結果は同じでコストだけ省きます。
8. **置換対象が複数箇所に一致する編集は失敗扱い**: 誤った箇所の書き換えを防ぐため。
9. **長い実行記録は先頭と末尾を残して切り詰め**: 最終回答が末尾にあるため。
10. **採用された提案は PURPOSE.md の履歴に採否とスコアを 1 行追記**し、skill-impact.md には提案の PURPOSE.md も記録します。
11. **CLI 障害は採点しない**（4 章参照）。

論文にない追加機能: 人間レビュー（ハイブリッド / 毎回）、`script`・`llm_judge` などの採点方法、途中再開、
`evaluate` / `compare` / `export` コマンド。`llm_judge` と人間レビューでの採点は論文では評価されていないため、
採点のぶれが検証ゲートの判断に影響しうる点に注意してください（val を多めにする、採点基準を具体的に書く、など）。

論文が挙げる限界（スキル検索をしない全文埋め込み、wiki の自動整理がない、など）はこの実装にもそのまま当てはまります。
