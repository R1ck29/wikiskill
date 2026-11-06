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

`improve` で実際の利用の記録が train / val に加わると、同じワークスペースで次の**ラウンド**が始まります。
スキル・wiki・raw はそのまま残し、今のスキルで val を解き直したスコアを新しい R_best にして、反復番号を続けたまま K 回進めます
（state.json の履歴に `new_tasks`、skill-impact.md に「タスクが変わったので以前のスコアとは比べられない」旨を記録）。
`improve` はワークスペースのロックを持ったまま「取り込み → ラウンド」を繰り返すので、実行中に記録がたまれば続けて次のラウンドに入ります。

利用者が何もしなくても回るように、`improve` は成功時に設定をスイートの `improve.json` に保存し、`feedback` は記録が
`--min-new` 件に達すると同じ `improve` を `--wait` 付きの独立プロセスとして起動します（出力は `background.log`）。
ワークスペースのロック（`evolve.lock`）は OS のファイルロックなので、強制終了やクラッシュでもプロセスが終われば外れ、手で消す必要はありません。
`--wait` の実行は `improve.wait` のロックを持って 1 つだけ順番待ちし、2 つ目は「先に待っている実行が新しい記録も取り込む」ので何もせず終わります。
自動の実行は claude ではモデル `sonnet`（別名なので Bedrock や社内ゲートウェイでもその環境の Sonnet に解決される）を使い、1 日の上限（`--daily-budget`、既定 10 ドル）を `backends.Budget` が守ります。
各呼び出しが報告する費用をワークスペースの `spend.json` に足し、上限に達すると次の呼び出しの前に `BudgetExceeded` で止めます
（再試行も採点もせず、完了した段階はそのまま残るので、次の実行が続きから進めます）。途中で止まったラウンドがあれば、`feedback` は
`--min-new` 件を待たずに次の 1 件で `improve` を起動します（`improve.unfinished_round`）。費用を報告しない codex は 1 日 1 回にします。
ラウンドの途中で別のモデル（または別の採点モデル）に変わったときは、ゲートを通す前に（その反復のゲートが決まった後なら次の反復の前に）今のスキルで val を採点し直して R_best にするので、
ゲートが別のモデルの点数と比べることはありません。失敗した `improve` は `improve.json` に時刻と原因を残し、次の `feedback` が知らせます（上限で止まった実行はそれを消さず、最後まで進んだ実行が消します）。
自動の実行は隠しオプション `--automatic` 付きで起動され、保存済みの設定を書き換えず、`improve.json` が消されていれば作り直しません。
子プロセスの stderr は起動のたびに空にする `background.err` に向けるので、`cli.main` がログを開く前の失敗（壊れた Python や読み込めないパッケージなど）も `feedback` が知らせます。
自動の実行は `--log` で受け取った `background.log` を自分で追記モードで開くので（Windows でも末尾に書く）、起動のたびに 1 MB を超えた分を末尾 256 KB に詰められます。
Windows では隠れたコンソールで起動するので、CLI を呼ぶたびにウィンドウが開くことはありません。
記録の追加と取り込みは短いロック（スイートの `feedback.lock`）を順番に取るため、取り込み中に届いた記録も失われません（クラッシュで途中まで書かれた行は読み飛ばし、残りの記録は使います）。
ファイルロックが使えないドライブ（一部のネットワークドライブ）では、待ち続けずに理由を示して止まります。
`feedback` の記録は、正解があれば（good の結果、または bad で示された正解）その正解に、正解の無い bad は「この応答は違うと言われた」という採点基準になります。後者は採点方法が `llm_judge` か `script` のスイートでだけ記録できます。
自動の実行はワークスペースごとの上限に加えて、すべての自動の実行の合計（`improve.total_budget`、既定 1 日 20 ドル、`budget --total` で `~/wikiskill-work/settings.json` に保存、
台帳は `~/wikiskill-work/spend-total.json`）でも止まります。
台帳の更新は OS のファイルロックの中で行うので、別の業務の実行と同時に書いても費用が消えません（ロックが使えないドライブではロックなしで数えます）。
書き出し（`improve --export`）が上書きするのは、WikiSkill が書き出したスキル（`skills.exported_by_wikiskill`）だけです。

### タスクの自動振り分け

論文は train / val / test を前もって固定し（どのベンチマークも train:val がおよそ 2:1）、test を大きく取って結果の報告だけに使います。
実務ではタスクが少しずつ増え、分け方を毎回考えるのは負担なので、`split` を書かないタスクは WikiSkill が決めます（`splits.py`）。

- **いつ**: スイートを読み込むとき（`config.load_suite`）と、評価の記録を取り込むとき（`improve.merge_pending`）。決めた split は tasks.jsonl に書き戻し、
  後から動かさない（動くと val の点数を比べられなくなる）。手で書いた split はそのまま使う。最初の書き換えの前に `tasks.jsonl.bak` を残す。
- **train と val**: 2:1（論文の比率）。評価の記録は「良い」「違う」ごとに 2:1 に分けるので、利用者が違うと言った結果も val に入り、
  ゲートが「簡単な依頼だけで満点」になりにくい。分けた結果 train か val が空になるときは、新しいタスクを 1 件（test に入ったものを優先して）移し、ラウンドを始められるようにする。
- **test**: タスクが `TEST_FROM`（40）件になってから、5 件に 1 件だけ。test は育成に使わず、依頼されたときの `evaluate`・`compare` にしか使わないので、
  少ないうちは学習と、ぶれにくいゲートに回す方が役に立つ。40 件の時点で val はおよそ 13 件（論文の最小の val 10 件を超える）。
- **順番**: 書かれた順ではなく id のハッシュ順に振り分ける（月ごとに並んだ例が train と val に偏らないように）。どの環境でも同じ結果になる。

### 例が無い業務の始め方

論文は正解付きの train / val / test を前もって用意し（例: train 16〜80、val 10〜40 件）、スキルも wiki も空から育てます。
利用中の評価から学ぶ仕組みは論文にはありません。WikiSkill は、利用者が評価した実際の依頼をそのまま正解付きのタスクにすることで、
例を用意しなくても論文と同じループを回します。

1. `init --workspace W [--export DEST]` が、タスクの無いスイート（空の tasks.jsonl）と `improve.json` を作る。手動の `improve` を実行していなくても、
   `feedback` が `improve` を自動で起動できるようになる。
2. 評価の記録は上の「タスクの自動振り分け」で train と val に分かれる。最初のラウンドは記録が `FIRST_ROUND`（6）件、つまり train 4・val 2 件そろうまで待つ
   （良い・違うの内訳によらず 4 と 2。`improve.rows_needed` / `merge_ready`。train か val だけ書かれたスイートは足りない側が 1 件入れば始まる）。取り込んだ後にラウンドが始まる前に止まった `improve` は、次の記録 1 件で起動し直す（`improve.unfinished_round`）。4 件は、スキル提案者が実行記録を 4 件以上読むという規則（付録 E.3 の規則 4）を満たす最小の数。
3. 最初のラウンドは論文と同じく、スキルなしで val を解いた点数を R_best にして始める。以後は `--min-new`（3）件ごとに新しいラウンドに入り、
   記録は同じ振り分けで train / val（40 件目からは test にも）に加わる。
4. どの業務のスイートに記録するかは、スキルの手順書に従って Claude が決める（`list` で既存の業務を見て、入力・出力・良し悪しの基準が同じなら同じスイート）。
   新しい業務を作るときだけ利用者に確認する。

この始め方の限界:

- 育成では依頼文と添付ファイルだけを空のフォルダに置いて解き直すので、リポジトリのコード・Web・社内システム・MCP に頼った作業は学べない。
- 最初の val は 2 件なので、採否が偶然に左右されやすい（論文も付録 B で、val が少ないと採否判定にノイズが入ると述べている）。
- 正解のない文章の業務では、採点基準を Claude が会話から書くので、例を用意した場合より採点がぶれやすい。
- 「良い」だけが続き、スキルなしで val が満点なら、育てるものが無いので何もしない（Algorithm 1 の 4 行目）。
- 逆に「違う」（直しの指示も含む）だけでも育つ。直しと正しい結果がそろえば正解付きのタスクになり、スキルなしでは解けなかった例なので学ぶことが多い。
  ただし val も最初に間違えた例だけになるため、もともと正しくできていた依頼で悪くならないかは確かめられない。「良い」も時々言ってもらうとその確認になる。
- 黙って使われた結果は記録しない。誤った正解が val に入ると採否の判定が狂うため。
- 評価かどうか、直しの指示から正解が何かは Claude が会話から判断するので、取り違えて誤った記録を残すことがある。
  気づいたら `forget` で取り消せる（取り込み済みの記録は、改善の最中でないときだけ）。気づかれない誤りは採否の判定を狂わせ続ける。
- フックが拾うのは評価や直しらしい言葉を含む発言だけで、見逃しや誤って反応する割合は実際の利用では測っていない。

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
  evolve.py       Algorithm 1 の反復ループとステージ管理（再開・人のレビュー待ち・タスク追加後の続行・モデルが変わったときの R_best の採点し直し・ワークスペースのロック）
  rollout.py      推論エージェントの実行（一時フォルダ・並列実行・結果キャッシュ・実行記録の整形）
  maintainer.py   Wiki メンテナ（記録の抽出、プロンプト作成、JSON 編集の適用）
  proposer.py     スキル提案者（ReAct ループ、読み取り専用サンドボックス、提案の検証）
  scoring.py      採点（exact/contains/regex/number/script/llm_judge）とレビューシート
  review.py       ハイブリッド方式（1 回だけ人が確認 → 正解データ化）
  improve.py      実際の利用からの改善（feedback の記録と取り消し（forget）、タスクへの取り込み、improve の自動起動とそのモデル・上限（全体の上限は budget で変更）、失敗の記録、ログの切り詰め、フッター付きの書き出し）
  evaluate.py     任意のスキルでの評価と、対応ありブートストラップ検定
  skills.py       SKILL.md の検証（Agent Skills 仕様）、プロンプトへの埋め込み、提案の適用
  patching.py     append / replace / insert_after の部分編集と差分（wiki とスキルで共用）
  workspace.py    3 層のフォルダ構成、raw の書き込み一回限り保証、state.json、OS のファイルロックと順番待ち、旧 gen-NNN 形式の解決
  config.py       suite.json / tasks.jsonl の読み込みと検証
  splits.py       split を書かないタスクの train / val / test の自動振り分け
  backends.py     claude / codex CLI の呼び出し（タイムアウト・中断時のプロセス停止、再試行、報告された費用での 1 日の上限）
  util.py         UTF-8 入出力、OS のファイルロック、LLM 出力からの JSON 抽出、切り詰め、CSV のエスケープ
  prompts/        Wiki メンテナとスキル提案者のシステムプロンプト（論文付録 E.2 / E.3）
```

配布と組み込みのためのファイル（README の「入れ方」）:

```
.claude-plugin/plugin.json, marketplace.json   リポジトリ全体を 1 つのプラグイン兼マーケットプレイスにする
skills/wikiskill/SKILL.md                      Claude が CLI を代わりに実行するための手順書（呼び出し時だけ読み込まれる）
skills/wikiskill/scripts/run_wikiskill.py      起動スクリプト。スキルフォルダ内 → 親フォルダの順に wikiskill パッケージを探し、
                                               Python が 3.9 未満なら PATH 上の新しい python3.X で実行し直す
skills/wikiskill/scripts/judgement_hook.py     UserPromptSubmit フック。評価らしい発言のときだけ「評価なら B に従って記録」という注意を
                                               Claude に渡す（判断は Claude。エラーでも発言は止めない。パッケージを読み込まないので速い）
hooks/hooks.json                               プラグインとして入れたときの同じフック
tools/integrate.py                             グローバル設定・プラグイン・プロジェクトへ、パッケージ・docs・examples を同梱した
                                               自己完結のスキルフォルダをコピー（全体を作ってから入れ替えるので、失敗しても前の版が残る）し、
                                               上のフックを設定ファイルに登録する（`--no-hook` で外す）
CLAUDE.md, INSTALL_PROMPT.md                   Claude に組み込みを頼むときの手順と、利用者が貼り付ける文章
```

依存の向きは `cli → improve → evolve/evaluate/review → rollout/maintainer/proposer → scoring/skills/backends → config/patching/workspace → splits/util` の一方向です。

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
`evaluate` / `compare` / `export` コマンド、実際の利用からの改善（`feedback`・`forget`・`budget` と `improve`）、例を用意しない始め方（`init --workspace`、`list`）、
train / val / test の自動の振り分け（`splits.py`）、評価を知らせるフック（`judgement_hook.py`）。論文が挙げる限界（スキル検索をしない全文埋め込み、wiki の自動整理がない、
毎回の改善を必須とする厳しい採否基準など）はこの実装にもそのまま当てはまります。
