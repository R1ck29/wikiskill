# WikiSkill 再設計（設計書）

- 対象: wikiskill 0.11.0（`main` の 0d9a101）からの再設計
- 状態: 第 0 段階（この PR）を実装中（進み具合は 9 章のチェック欄）。第 1 段階以降は 9 章の作業指示に従って別の PR で実装する
- 実測した環境: Linux、Python 3.13.5、Claude Code 2.1.325、codex-cli 0.169.0（2026-11-17）

この文書は、後続の段階をほかの実装者（別のモデルを含む）がこの文書だけで進められるように書いています。
モジュールの境界・関数の形・データ形式・移行手順・テスト計画を 6〜9 章にまとめました。

## 1. 結論

1. **CLI の呼び出しを「アダプター層」に切り出す。** 引数の組み立て・出力の解析・エラーの分類・バージョンと機能の判定を
   `wikiskill/agent_cli/`（WikiSkill に依存しない純粋な関数の集まり）に移し、`backends.py` は再試行・上限・設定ファイルの互換だけを持つ薄い層にする。
   `agent_cli` は、承認済みの共有ライブラリ `agent_cli` に置き換える前提の先行実装で、求める仕様は 7 章にまとめた。
2. **公開している形は変えない。** CLI のオプション、バックエンド設定ファイルのキー、ワークスペースのフォルダ構成、`state.json`・`spend.json`・`improve.json` の形式はそのまま。
   したがって版は **0.12.0（マイナー）** とし、`docs/MIGRATION.md` と `migrate` コマンドは作らない（理由は 8 章）。
3. **ワークスペース形式 v2 は作らない。** wiki の整理（lint・圧縮・統合）は、既存の形式にファイルを足すだけで実現できる（6.5 節）。
   形式を変える利点が、導入済みのワークスペースを移行する手間を上回らない。
4. **wiki の整理は 3 段に分ける。** (a) モデルを呼ばない lint、(b) プログラムが書いたファイルの決定的な圧縮（古い分は `wiki/archive/` に退避）、
   (c) モデルによるパターンの統合（lint を通った場合だけ採用）。この PR では設計だけを行い、実装は第 2〜3 段階に回す。

## 2. 現在のアーキテクチャ（0.11.0）

全体は [ARCHITECTURE.md](../ARCHITECTURE.md) のとおり。再設計に関係する部分だけを抜き出す。

```
cli.py (580 行)  ── コマンド定義。上限（Budget）の組み立て、自動実行の設定の保存もここ
  └ improve.py (462 行) ── feedback の記録、自動起動、全体の上限（total_budget）、設定ファイル 2 種の読み書き
      └ evolve.py / evaluate.py / review.py
          └ rollout.py / maintainer.py / proposer.py
              └ backends.py (493 行) ── 引数の一覧（DEFAULTS）、プロセス実行、再試行、Budget、claude/codex の出力解析
```

モデル呼び出し 1 回の流れ（`backends.py`）:

1. `make_backend` が `DEFAULTS[kind]` を設定ファイルで上書きして `BackendConfig` を作る（`backends.py:183-202`）。
2. `complete` / `run_agent` が引数を組み立てる。固定の一覧 + モデル + システムプロンプトのファイル（`backends.py:374-384, 444-448`）。
3. `_call` が上限を確かめ、プロセスを実行し、費用を台帳に足し、出力を解析する（`backends.py:231-239`）。
4. 失敗（`BackendError`）は種類を問わず `retries` 回やり直す（`backends.py:253-263`）。`run_agent` は最後の失敗を `infra_error` として返す。

## 3. 問題点と根拠

| # | 問題 | 根拠 |
|---|---|---|
| P1 | 引数の一覧がコードに固定で、確認した版が古い | `backends.py:44-64`（「claude 2.1.295 / codex-cli 0.146.1 で確認」）。CLI が変わるたびに利用者が設定ファイルで丸ごと書き換える必要がある（`docs/CONFIGURATION.md` 3 章、4 章「`--safe-mode` が使えない CLI」） |
| P2 | 失敗を区別せず、すべて同じ回数だけやり直す | `backends.py:253-263`。認証切れや利用上限は待っても直らないのに、タスクごとに 3 回（10 秒・20 秒待ち）試す。4 章の実測 M2・M3 |
| P3 | 利用上限・認証切れが「CLI の障害」として報告され、何をすればよいか伝わらない | `rollout.py:127-131` は「同じコマンドを再実行」としか言わない。codex では失敗した呼び出しが `unpriced_calls` に数えられる（`backends.py:237-238`） |
| P4 | 1 回の呼び出しの費用に上限がない | `Budget.check` は呼び出しの前にしか見ない（`backends.py:128-133`）。エージェント実行 1 回（既定 30 分）と並列数 4 の分だけ、1 日の上限を超えうる |
| P5 | claude のエージェント実行で使える道具が増えている | 4 章の実測 M4。2.1.325 では `CronCreate`・`ScheduleWakeup`・`PushNotification`・`SendMessage` などが `--safe-mode` でも使える。禁止しているのは `WebFetch,WebSearch` だけ（`backends.py:54`） |
| P6 | 上限と設定の処理が 3 か所に散っている | `Budget` は `backends.py:85-158`、全体の上限と `settings.json` は `improve.py:384-414`、組み立ては `cli.py:344-345`。自動実行の方針（モデル・上限）は `improve.py:347-360` |
| P7 | wiki が増え続ける | `maintainer.py:42-46` は wiki 全体（`index.md`・`logs.md`・`skill-impact.md`・全パターン）を毎回プロンプトに入れる。`skill-impact.md` は反復ごとに差分と PURPOSE.md を全文で追記する（`evolve.py:286-303`）。`improve` は同じワークスペースで反復を続けるので（論文の K=8 と違い）上限がない。論文も wiki の自動整理がないことを限界に挙げている（`docs/ARCHITECTURE.md:195-196`） |
| P8 | スキルを定期実行・`/loop` から使うと確認が出る | Claude Code 2.1.325:「Changed scheduled tasks and `/loop` to ask before running a skill that declares `allowed-tools` or hooks; add `Skill(name)` to `permissions.allow` to run it unattended」。`skills/wikiskill/SKILL.md:5` は `allowed-tools` を宣言している |
| P9 | サンドボックスを有効にした利用者では、スキルの Bash 実行に確認が出る | Claude Code 2.1.325:「Changed sandboxed Bash commands to follow the normal permission flow by default; set `sandbox.autoAllowBashIfSandboxed: true` to keep running them without prompts」 |
| P10 | 大きいモジュールに役割が混ざっている | `cli.py` は引数の定義・コマンドの本体・自動実行の設定の保存を持つ。`improve.py` は記録・取り込み・自動起動・上限・書き出しを持つ。`backends.py` は P1〜P4 と Budget を持つ |

「確認した版が古い」こと自体は、今回の実測では**既存の引数がすべて今の CLI でも受け付けられる**ことがわかった（M1）。
壊れていたのは引数ではなく、失敗の扱い（P2・P3）と、CLI 側で増えた機能への追従（P5・P8・P9）である。

## 4. 実測の記録

2026-11-17、Linux、`claude 2.1.325 (Claude Code)`、`codex-cli 0.169.0`。
この環境では claude は未ログイン、codex は利用上限に達していたため、モデルの応答は得られていない。
引数が受け付けられるか・失敗がどういう形で返るかは確認できた。

| # | 実行したもの | 結果 |
|---|---|---|
| M1 | 0.11.0 の引数の一覧そのまま（claude のテキスト生成・エージェント実行、codex のテキスト生成）。`--system-prompt-file`・`--append-system-prompt-file` を含む | どれも引数の誤りにならず、API を呼ぶ段階まで進む。存在しない引数は `error: unknown option '--no-such-flag'` |
| M2 | claude、未ログイン | 終了コード 1。JSON は `is_error: true`、`subtype: "success"`、`terminal_reason: "api_error"`、`api_error_status: null`、`result: "Not logged in · Please run /login"`、`total_cost_usd: 0`。stream-json では assistant イベントに `error: "authentication_failed"` と `is_api_error_message: true` が付く |
| M3 | codex、利用上限 | 終了コード 1。`{"type":"error","message":"You’ve hit your usage limit. … try again at 4:26 AM."}` の後に同じ文の `turn.failed`。stderr は空 |
| M4 | claude `--safe-mode --permission-mode bypassPermissions --disallowedTools WebFetch,WebSearch` の `system/init` イベント | `tools` は 22 個: Task, Bash, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree, ExitWorktree, ListAgents, Monitor, NotebookEdit, PushNotification, Read, ReportFindings, ScheduleWakeup, SendMessage, Skill, TaskStop, ToolSearch, Workflow, Write。`skills` は組み込みの 18 個だけ、`mcp_servers` は空（個人のスキルと MCP は読み込まれていない）。`claude_code_version: "2.1.325"` が入る |
| M5 | stream-json の先頭 | `system/init` の前に `{"type":"system","subtype":"ui_invalidate"}` が出る（既存の解析は assistant / user / result 以外を無視するので影響なし） |
| M6 | Claude Code 自身の利用上限の文面（この作業中に別の実行で発生） | `You've hit your 5-hour limit · resets 5:10am (Asia/Tokyo)`、エラー種別は `rate_limit`、HTTP 429。**HTTP 429 だけでは一時的な混雑と利用上限を区別できない**ので、文面で見分ける必要がある |
| M7 | `codex exec --help`（0.169.0） | 新しい引数: `--trust`（「Save trust for this project and future chats … Persists even with --ephemeral」）、`--ignore-user-config`（「Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`」）、`--ignore-rules`、`--strict-config`、`--dangerously-bypass-hook-trust`、`--approve-for-me`。`--full-auto` は一覧にない |

追加の実測（禁止する道具の指定、`--tools ""` の効き方、codex の設定キー）は実装時に行い、この表に追記する。

### Claude Code 2.1.296〜2.1.325 の変更のうち関係するもの

出典は公式の変更履歴（https://code.claude.com/docs/en/changelog）と公式ドキュメント。

| 版 | 変更（原文） | WikiSkill への影響 |
|---|---|---|
| 2.1.325 | Changed scheduled tasks and `/loop` to ask before running a skill that declares `allowed-tools` or hooks; add `Skill(name)` to `permissions.allow` to run it unattended | P8。組み込み時に `Skill(wikiskill)` を許可する選択肢を用意する（6.7 節） |
| 2.1.325 | Changed sandboxed Bash commands to follow the normal permission flow by default; set `sandbox.autoAllowBashIfSandboxed: true` to keep running them without prompts | P9。エージェント実行（`bypassPermissions`）には影響しない。スキルから `run_wikiskill.py` を実行するときに確認が出る |
| 2.1.325 | Changed `claude -p` with no cached account data to wait for that data before its first request | 初回の呼び出しが数秒遅くなる。既定のタイムアウト（1800 秒）で足りる |
| 2.1.323 | Added `engines["claude-code"]` to `plugin.json`: plugins outside their stated Claude Code version range are not installed, updated or loaded | 使わない（古い CLI でも読み込めるほうがよい。版ごとの違いはアダプターが吸収する） |
| 2.1.323 | Improved the Fable limit message in `-p`, SDK, IDE and cloud sessions: it now says when the limit resets | 利用上限は構造化された項目ではなく文面で届く（M6）。文面から分類する |
| 2.1.318 | Added … to `claude -p` JSON results (`credential_expires_at`) | `check` で期限が近いことを知らせられる（第 1 段階） |
| 2.1.316 | Fixed `claude -p` exiting with code 0 and no output when startup failed; it now prints the error and exits 1 | 出力が空で終了コード 1 の場合がある。stderr の文面で分類する |
| 2.1.311 | Changed `--output-format json` and `stream-json` output to write DEL, C1 control and Unicode format characters inside strings as `\uXXXX` escapes | JSON として解析しているので影響なし |
| 2.1.302 | Changed `claude -p` … with no permission mode to start in `default` mode on third-party providers | エージェント実行は権限モードを明示しているので影響なし |
| （ドキュメント） | `--bare` is the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future release | **今後の最大のリスク。** `--bare` は OAuth とキーチェーンを読まないので、サブスクリプションでログインした利用者の呼び出しが止まる。既定になった版で、打ち消す引数を機能表に足す（6.2 節） |
| （ドキュメント） | `claude -p` は、ターンが終わってもバックグラウンドの作業を最大 10 分待つ（`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`） | エージェントがバックグラウンドでコマンドを始めると 1 件が最大 10 分延びる。既定のタイムアウト内 |
| （ドキュメント） | `system/api_retry` の `error` は `authentication_failed`・`billing_error`・`rate_limit`・`overloaded`・`invalid_request`・`model_not_found`・`server_error` など | 分類の語彙として採用する（6.2 節） |

2.1.296〜2.1.325 の間に、WikiSkill が使う引数（`--safe-mode`・`--tools`・`--disallowedTools`・`--permission-mode`・`--no-session-persistence`・
`--system-prompt-file`・`--append-system-prompt-file`・`--output-format`・`--verbose`・`--model`）の改名・廃止・意味の変更は見つからなかった。
`--safe-mode` は 2.1.169、`--max-budget-usd` は 2.0.28、`--bare` は 2.1.81 で追加されている。

### Codex 0.146.1 → 0.169.0

- **`--trust` は付けない。** 信頼していないフォルダでは、そのフォルダの設定と MCP が読まれない。WikiSkill のエージェント実行は、
  タスクの入力ファイルだけを置いた一時フォルダで動くので、読まれないほうが正しい。`--trust` は信頼を**保存する**（`--ephemeral` でも残る）ため、
  付けると一時フォルダの記録が利用者の設定にたまる。
- **`--ignore-user-config` は既定にしない。** 個人設定を読まない「`--safe-mode` に当たる指定」が 0.169 で使えるようになったが、
  社内ゲートウェイや Azure の接続先（`model_provider`）は `config.toml` に書くので、既定で無視すると認証先が変わって動かなくなる。
  実験を純粋にしたい利用者は設定ファイルで足せる（`docs/CONFIGURATION.md` 4 章）。
- 0.11.0 の引数はそのまま受け付けられた（M1）。利用上限は M3 の形で届く。

## 5. 選択肢

| 案 | 内容 | 利点 | 欠点 | 判断 |
|---|---|---|---|---|
| A. 小さな修正 | `DEFAULTS` を今の版で確認し直し、コメントを更新する | 変更が最小 | CLI が変わるたびに同じ作業が必要。P2〜P4 は直らない。共有ライブラリにもつながらない | 不採用 |
| B. アダプター層 + 追加だけの wiki 整理 | 6 章のとおり。公開している形は変えない | P1〜P6 を直し、P7 も形式を変えずに直せる。既存の導入先はそのまま更新できる | `backends.py` の中身は大きく変わる（外から見える名前は残す） | **採用** |
| C. CLI をやめて SDK / API を直接呼ぶ | Agent SDK や HTTP API に切り替える | 出力の形が安定する | 「標準ライブラリだけ」「CLI の認証をそのまま使う」「WikiSkill 自身は通信しない」（`tests/test_no_network.py`）という前提を崩す。Bedrock・ゲートウェイの利用者は設定し直しになる | 不採用 |
| D. ワークスペース形式 v2 | パターンのページにメタデータを持たせ、形式の番号を `state.json` に入れる | wiki の整理をきれいに書ける | 論文のプロンプト（付録 E.2 / E.3）が前提にしている wiki の形を変える。導入済みのワークスペースすべてに移行が要る。得られる機能は追加のファイルでも実現できる | 不採用（必要になったときの入れ方は 6.6 節） |

## 6. 目標アーキテクチャ

### 6.1 層

```
cli.py            コマンドの定義と表示だけ（第 4 段階で commands/ に分割）
improve.py        feedback の記録・取り込み・自動起動（上限は budget.py へ）
budget.py         上限と台帳（Budget, BudgetExceeded, 全体の上限）            ← 第 1 段階
wiki_lint.py      wiki の lint と決定的な圧縮                                ← 第 2 段階
consolidator.py   モデルによるパターンの統合                                 ← 第 3 段階
backends.py       WikiSkill 向けの窓口: 設定ファイル、再試行の方針、上限との連携、プロセス実行
agent_cli/        CLI ごとの知識: 版と機能の判定、引数の組み立て、出力の解析、エラーの分類（WikiSkill を import しない）
```

依存の向きは今までと同じ一方向で、`agent_cli` がいちばん下に入る。

### 6.2 `wikiskill/agent_cli/`（この PR で実装）

入出力を持たない関数だけで構成する（プロセスの実行は呼び出し側が行う）。こうしておくと、テストは文字列を渡すだけで書け、
共有ライブラリへの置き換えもファイル単位でできる。

```python
# agent_cli/__init__.py が公開する名前
KINDS = ("claude", "codex")

@dataclass(frozen=True)
class CliInfo:
    kind: str                          # "claude" | "codex"
    version: tuple[int, ...] | None    # (2, 1, 325)。読み取れなければ None
    raw: str                           # `--version` の 1 行目
    def has(self, capability: str) -> bool: ...
    def label(self) -> str: ...        # "claude 2.1.325" / "codex (version unknown)"

VERSION_ARGS = ("--version",)
def parse_version(kind: str, output: str) -> CliInfo      # 形式が違えば version=None
def unknown(kind: str) -> CliInfo                         # 版を調べていないとき

@dataclass(frozen=True)
class CallSpec:
    mode: str                          # "complete"（道具なしで 1 回答える）| "agent"（作業フォルダで道具を使う）
    model: str | None = None
    system_file: str | None = None     # システムプロンプトを書いたファイル（claude）
    cwd: str | None = None             # 作業フォルダ（codex の -C）
    max_cost_usd: float | None = None  # この 1 回の費用の上限（対応する CLI だけ）

def base_args(kind: str, mode: str, info: CliInfo) -> tuple[str, ...]   # 版に合わせた固定部分
def call_args(kind: str, spec: CallSpec, base: Sequence[str], info: CliInfo, model_flag: str) -> list[str]
def join_prompt(kind: str, system: str | None, prompt: str) -> str      # codex は system を先頭に付ける

@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None      # CLI が報告した費用。報告しなければ None

@dataclass(frozen=True)
class CallError:
    kind: str                          # 下の表
    message: str
    @property
    def retryable(self) -> bool: ...
    @property
    def fatal(self) -> bool: ...       # 実行全体を止めるべきもの

@dataclass(frozen=True)
class Outcome:
    text: str                          # 最終回答
    trace: str                         # 読める形の実行記録（Markdown）
    usage: Usage | None
    error: CallError | None
    cli_version: str | None            # 出力に含まれていれば（claude の system/init）
    finished: bool                     # 結果のイベントまで届いたか

def parse_output(kind: str, mode: str, stdout: str) -> Outcome
def classify_exit(kind: str, returncode: int, stderr: str) -> CallError   # 出力が無いまま終了したとき
```

エラーの種類（`CallError.kind`）:

| 種類 | 意味 | やり直す | 実行全体を止める | 見分け方 |
|---|---|---|---|---|
| `auth` | 未ログイン・認証切れ | × | ○ | claude: assistant の `error` が `authentication_failed` など、文面 `Not logged in`・`/login`・`Invalid API key`・`OAuth`、状態 401/403。codex: `401`・`Unauthorized`・`login` |
| `usage_limit` | 契約の利用上限（リセット時刻まで待つしかない） | × | ○ | 文面 `usage limit`・`hit your … limit`・`limit reached`・`resets`（M3・M6）。**状態 429 より先に文面で判定する** |
| `billing` | 残高不足・請求の問題 | × | ○ | `billing_error`、`Credit balance` |
| `cost_cap` | 1 回の費用の上限（`--max-budget-usd`）に達した | × | ○（Budget の上限として扱う） | claude の `subtype` が `error_max_budget_usd` |
| `bad_option` | CLI が引数を受け付けない（版の違い） | 版を調べ直して 1 回だけ | 直らなければ ○ | 出力が無く、stderr に `unknown option`・`unexpected argument`・`unrecognized` |
| `bad_model` | モデル名が使えない | × | ○ | `model_not_found`、`model is not supported`、`does not exist` |
| `rate_limit` | 一時的な混雑（429） | ○ | × | 状態 429、`rate_limit`、`Too many requests` |
| `overloaded` | サーバー側の障害（5xx・529） | ○ | × | `overloaded`、`server_error`、状態 500 以上 |
| `incomplete` | 出力が途中で切れた・結果のイベントが無い | ○ | × | 解析結果 |
| `unknown` | 上のどれでもない | ○ | × | 既定（0.11.0 と同じ扱い） |

機能表（`agent_cli/capabilities.py`）は「機能名 → その CLI で使える最初の版」の辞書で、版を調べた結果と突き合わせる。
**版がわからないとき（調べていない・読み取れない）は 0.11.0 と同じ引数を使う。** これで、版の判定が外れても今より悪くならない。

| CLI | 機能名 | 最初の版 | 使い方 |
|---|---|---|---|
| claude | `safe_mode` | 2.1.169 | 無ければ `--safe-mode` を外す（今は利用者が設定ファイルで外している） |
| claude | `max_budget_usd` | 2.0.28 | 上限つきの実行で、残りの金額を `--max-budget-usd` に渡す（P4） |
| claude | `scheduling_tools` | 版を確認できたとき | エージェント実行で、作業フォルダの外や実行の後に作用する道具（`CronCreate`・`CronDelete`・`CronList`・`ScheduleWakeup`・`PushNotification`・`SendMessage`・`RemoteTrigger`）を禁止に加える（P5） |
| claude | `bare_default` | 未定（将来） | `-p` の既定が `--bare` になった版で、打ち消す引数を足す |
| codex | `ignore_user_config` | 0.169.0 で確認（導入された版は未確認） | 既定では使わない（4 章） |

新しい版の CLI で引数を変える必要が出たら、この表と `base_args` を直すだけで済む。`backends.py` と上の層は触らない。

### 6.3 `backends.py`（この PR で実装）

外から使われている名前はすべて残す: `make_backend`・`load_backend_config`・`BackendConfig`・`CLIBackend`・`ClaudeBackend`・`CodexBackend`・
`AgentResult`・`BackendError`・`StoppedError`・`Budget`・`BudgetExceeded`・`run_process`・`kill_all_running`・`reset_stop`・`DEFAULTS`。

**引数の決まり方は 2 通り。**

- **自動（既定）**: 設定ファイルに `complete_args` / `agent_args` を書いていないとき。`agent_cli.base_args` が版に合わせて決める。
- **固定**: 設定ファイルに書いたとき。0.11.0 と同じく**書いたとおりに丸ごと使い**、版に合わせた調整（道具の禁止の追加・`--max-budget-usd`）は行わない。
  古い設定ファイルはそのまま動く。

**版の判定。** `CLIBackend.detect()` が `<command> --version` を 1 回実行して `CliInfo` を得る（15 秒で打ち切り、失敗したら「不明」）。
`cli.py` の各コマンドがモデルを呼ぶ前に呼ぶ。`check` は版・自動か固定か・使う引数を表示する。
`detect()` を呼ばずに使った場合（ライブラリとして使う・テスト）は 0.11.0 と同じ引数で動き、CLI が引数を拒んだとき（`bad_option`）にだけ版を調べ直して 1 回やり直す。

**失敗の扱い。**

```
BackendError(message, kind="unknown")     # .kind / .retryable / .fatal を持つ。今までの except BackendError はそのまま動く
  ├ StoppedError                          # Ctrl+C（今までどおり）
  └ FatalBackendError                     # auth / billing / bad_option / bad_model: やり直さず、run_agent からもそのまま送出
BudgetExceeded(RuntimeError)              # 今までどおり
  └ UsageLimitReached                     # usage_limit / cost_cap: `improve` は「paused」として扱い、続きから再開できる
```

- `retryable` な失敗だけを `retries` 回やり直す。待ち方は今までと同じ（`retry_wait × 回数`）。
- `fatal` な失敗は `run_agent` が `infra_error` に包まずに送出する。並列のロールアウトは最初の例外で残りを止める仕組み
  （`rollout.py:119-126`）をすでに持っているので、未ログインのまま全タスクを 3 回ずつ試すことがなくなる。
- 利用上限（`UsageLimitReached`）は `BudgetExceeded` の一種にする。`improve` はすでに `BudgetExceeded` を「今日は止めて、続きは次回」と扱っている
  （`cli.py:357-359`）ので、同じ経路で止まり、完了した段階は残る。
- 失敗した呼び出しは `unpriced_calls` に数えない（codex が上限で失敗しただけで「費用を報告しない CLI は 1 日 1 回」に当たるのを防ぐ）。

### 6.4 `budget.py`（第 1 段階）

`Budget`・`BudgetExceeded` を `backends.py` から、`total_budget`・`total_limit`・`set_total_limit`・`automatic_budget`・`AUTO_BUDGET`・
`AUTO_TOTAL_BUDGET`・`TOTAL_SETTINGS` を `improve.py` から移す。元のモジュールには再エクスポートを残す（テストは `from wikiskill.backends import Budget` を使う）。
台帳の形式（`spend.json`: `date`・`usd`・`unpriced_calls`・`runs`）は変えず、キーを足すだけにする（`tokens_in`・`tokens_out`: codex の利用量を数えるため）。
古い版が新しい台帳を読んでも、知らないキーを無視するだけで動く（`Budget.today` は知っているキーだけを読む）。

### 6.5 wiki の整理（第 2〜3 段階）

守ること: **wiki は消さない・巻き戻さない**（論文 §3.1）。整理で外したものは必ず `wiki/archive/` に残す。
`wiki/archive/` はメンテナにも提案者の最初の入力にも入れない（提案者は `read_file` で読める）。

**(a) lint（モデルを呼ばない）** `wikiskill lint <workspace>`（終了コード 0 = 問題なし、1 = 問題あり）と、各反復の maintain の後に自動で実行。

| 検査 | 内容 |
|---|---|
| `dead-link` | `index.md` が指す `wiki/patterns/<名前>.md` が無い |
| `orphan` | `patterns/` にあるのに `index.md` から指されていないページ |
| `duplicate` | ファイル名（ハイフン区切りの語の集合）や 1 行目の見出しがほぼ同じページの組（語の集合の Jaccard 係数 0.8 以上） |
| `oversize-page` | 1 ページが 8,000 字を超える |
| `oversize-wiki` | メンテナに渡す wiki 全体が 120,000 字を超える（15,000 字 × 8 件の実行記録と合わせてもモデルの入力に収まる目安） |
| `stale` | 直近 N 反復（既定 9 = 3 ラウンド）で一度も更新・言及されていないページ（`logs.md` と `raw/` の反復番号から判定） |
| `unknown-skill` | `skill-impact.md` 以外で、もう存在しないスキル名を「採用中」として書いているページ |

結果は `wiki/lint.json`（`{"iteration": k, "issues": [{"check": ..., "page": ..., "detail": ...}]}`）に書き、メンテナのプロンプトの末尾に
「直すべき点」として最大 10 件を渡す（メンテナは今の編集形式のままで直せる: `dead-link`・`orphan` は `update_index`、`oversize-page` は `update_patterns`）。

**(b) 決定的な圧縮（モデルを呼ばない）** プログラムが書いた部分だけを対象にする。

- `skill-impact.md`: 直近 6 反復の節は全文のまま残す。それより古い節は 1 行（`- Iteration 12: REJECTED modify report-total, val 0.6667 (best 0.8333)`）に置き換え、
  元の全文は `wiki/archive/skill-impact-<最初の反復>-<最後の反復>.md` に移す。節の区切りは `evolve._record_skill_impact` が書く見出し（`## Iteration k `）で、内容は `state.json` の `history` から再現できる。
- `logs.md`: 直近 12 反復の節（`## Iteration k`）を残し、古い節は `wiki/archive/logs-<範囲>.md` に移して、「反復 1〜20 の記録は archive にある」という 1 行を残す。
- 実行の時点: maintain の前。途中で止まってもやり直せるように、書き込みは「archive に書く → 本体を書き換える」の順で、archive に同じ節があれば書かない。

**(c) モデルによる統合（第 3 段階）** `oversize-wiki` か `duplicate` が出たとき、またはラウンドの最初にだけ動く。

- 入力: `index.md`、全パターン、lint の結果。出力はメンテナと同じ JSON に `retire_patterns: [{"name": ..., "merged_into": ..., "reason": ...}]` を足した形。
- 適用: 退役するページは `wiki/archive/patterns/` に移す（消さない）。統合先のページには `Merged from: <名前>` の行を必ず入れさせる。
- 採否: 適用後に lint をかけ、`dead-link` と `orphan` が 0 件で、退役したページのすべてが統合先から参照されているときだけ採用。そうでなければ統合前の wiki に戻す
  （maintain と同じく、適用前に `runs/iter_NNN/wiki_before_consolidation` に写しを取る）。
- 費用: テキスト生成 1 回。上限つきの実行では通常の呼び出しと同じく数える。

### 6.6 ワークスペースの形式

v1 のまま。足すのは `wiki/archive/`・`wiki/lint.json` と `spend.json` のキーだけで、古い版はこれらを無視して動く。
将来どうしても形式を変える必要が出たら、`state.json` に `"format": 2` を入れ、無い場合は 1 とみなし、読み込み時に自動で移行する
（旧 `gen-NNN` 形式を `workspace.resolve_workspace` が解決しているのと同じやり方）。そのときが 1.0.0 である。

### 6.7 スキル・プラグインとしての配布

- **定期実行・`/loop`（P8）**: `tools/integrate.py` に `--allow-skill` を足し、フックと同じ設定ファイルの `permissions.allow` に `Skill(wikiskill)`
  （プラグインは `Skill(wikiskill:wikiskill)`）を追加する。権限を広げる変更なので既定では行わず、付けなかったときは 1 行の案内を表示する（第 1 段階）。
- **サンドボックス（P9）**: スキルが実行する `run_wikiskill.py` は `claude` / `codex` を起動し、`~/wikiskill-work` と一時フォルダに書く。
  サンドボックスの中では通信と書き込みが止められるので、サンドボックスを使う利用者には `sandbox.excludedCommands` に登録する手順を
  `docs/CONFIGURATION.md` 4 章に書く（導入先で確かめる項目として 5 章にも足す）。
- **`engines`**: 使わない（4 章）。

## 7. agent_cli に求めるもの

共有ライブラリ `agent_cli`（標準ライブラリだけ・各リポジトリにコピーして使う）に、このリポジトリが求めることを挙げる。
6.2 節の先行実装（`wikiskill/agent_cli/`）がそのままたたき台になる。

**前提**

- Python 3.10 以上、標準ライブラリだけ。ネットワーク系のモジュール（`socket`・`urllib`・`http` など）を import しない（`tests/test_no_network.py` が `wikiskill/` 以下を検査する）。
- コピーして使うので、パッケージ内の import は相対 import だけにする。ライセンス表示のファイルを 1 つ同梱する。
- 中核は入出力を持たない関数にする（文字列を受け取り、データを返す）。プロセスの実行は別モジュールに分け、使うかどうかを利用側が選べるようにする。

**1. 版の判定**

- `parse_version(kind, output) -> CliInfo`。`2.1.325 (Claude Code)` と `codex-cli 0.169.0` を読む。形式が違えば `version=None`（例外にしない）。
- `--version` の実行は 1 プロセスにつき実行ファイルごとに 1 回で済むよう、結果を呼び出し側が持てること（ライブラリが勝手にファイルへ保存しない）。
- claude の stream-json の `system/init` にある `claude_code_version` からも版を得られること（追加の実行なしで確かめられる）。

**2. 版ごとの引数の組み立て**

- 意図を渡すと引数の一覧を返す: `mode`（`complete` / `agent`）、モデル、システムプロンプト（ファイルで渡す）、作業フォルダ、
  禁止する道具、権限の方針（`bypass` / `accept_edits + 許可する道具`）、個人設定を読むかどうか、1 回の費用の上限、出力の形式（解析できる JSON に固定）。
- **プロンプトは標準入力、長い文字列はファイルで渡す**（Windows のコマンド長の上限 32k 字と `.cmd` 経由での引用符の崩れを避ける）。空文字列の引数（`--tools ""`）が `.cmd` を通っても残ること。
- 版がわからないときに使う「確認済みの最低線」の引数を持つこと。新しい引数は、機能表でその版にあるとわかったときだけ使う。
- 機能表はデータ（辞書）で、1 行足せば新しい版に対応できること。「将来この版から既定が変わる」という予告（`--bare` の既定化）も表に書けること。
- 利用側が引数を丸ごと差し替えられること（WikiSkill の `complete_args` / `agent_args` との互換のため）。

**3. 出力の解析**

- claude: `--output-format json` の 1 つのオブジェクトと、`stream-json` の行。`result` イベント（`result`・`is_error`・`subtype`・`terminal_reason`・`api_error_status`・
  `total_cost_usd`・`usage`・`modelUsage`・`num_turns`・`credential_expires_at`）、`assistant` / `user` の内容ブロック（text・thinking・tool_use・tool_result）、
  `system/init`・`system/api_retry`・`rate_limit_event`。知らないイベントと知らない項目は無視する（M5）。
- codex: `--json` の行。`item.completed`（agent_message・reasoning・command_execution・file_change・mcp_tool_call・error ほか）、`turn.completed`（`usage`）、`turn.failed`、`error`。
- 途中で切れた出力（結果のイベントが無い、JSON でない行が混ざる）を例外にせず、`finished=False` として返すこと。
- 実行記録を、正規化したイベントの列（役割・種類・本文・道具の名前と入力・出力・エラーかどうか）として返すこと。Markdown への整形は利用側でもできるが、既定の整形も用意する
  （WikiSkill は `**tool call `Bash`:**` の形を wiki と提案者に見せているので、形を変えると既存のワークスペースの記録とそろわなくなる）。

**4. 利用量と上限の取り出し**

- `Usage`（入力・出力・キャッシュ読み書きのトークン数、報告された費用）。claude は `total_cost_usd` と `usage`、codex は `turn.completed.usage`（費用は無いので `None`）。
- 失敗した呼び出しでも、報告された費用があれば返すこと（失敗にも料金がかかる）。
- 利用上限のリセット時刻が文面にあれば、文字列のまま取り出せること（`try again at 4:26 AM`・`resets 5:10am (Asia/Tokyo)`）。時刻への変換は求めない（タイムゾーンが文面に無い場合がある）。
- `credential_expires_at`（claude 2.1.318 以降）を取り出せること。

**5. エラーの分類**

- 6.2 節の表の種類に分け、`retryable`（待てば直る）と `fatal`（実行全体を止める）を返すこと。
- 判定の材料は、構造化された項目（claude の assistant イベントの `error`、`api_error_status`、`subtype`、`terminal_reason`）を先に、文面を後に使う。
  ただし利用上限は HTTP 429 として届くので（M6）、**利用上限の文面は状態コードより先に見る**。
- 出力が無いまま終了した場合は、終了コードと stderr から分類する（`unknown option` → `bad_option`）。
- 分類できないものは `unknown`（やり直す）にする。新しい文面に出会っても今より悪くならないこと。
- 文面の規則は表（正規表現と種類の組）にして、テストで実際の文面（M2・M3・M6）を固定すること。

**6. プロセスの実行（別モジュール）**

- タイムアウトと中断のときに、子プロセスの木ごと止める（npm で入れた CLI は `.cmd` やラッパースクリプトなので、直接の子だけ止めても本体が残る。
  Windows は `taskkill /T /F`、それ以外はプロセスグループへの `SIGKILL`）。
- 実行中のプロセスの一覧と「これ以上始めない」という合図を持ち、Ctrl+C で全部を止められること（並列のロールアウト）。
- Windows でコンソールのウィンドウを開かない（`CREATE_NO_WINDOW`）。標準入出力は UTF-8、壊れた文字は置き換える。
- WikiSkill のテストは `backends.run_process`・`backends._kill_tree`・`backends._LIVE` を直接使っているので、
  ライブラリに移すときは「止める関数を引数で渡せる」「一覧を共有できる」形にする（`backends.py` に同じ名前の薄い関数を残す）。

**7. ライブラリに入れないもの**

再試行の回数と待ち方、1 日の上限と台帳、設定ファイルの読み込み、WikiSkill の `AgentResult`。これらは利用側（`backends.py`・`budget.py`）が持つ。

## 8. 互換性と移行

| 対象 | この PR での扱い |
|---|---|
| CLI のコマンドとオプション | 変更なし。追加もなし（`check` の表示が 2 行増える） |
| バックエンド設定ファイル（`--backend-config`） | キーは同じ。`complete_args` / `agent_args` を書いた設定は 0.11.0 と同じ引数になる（テストで固定） |
| `~/wikiskill-work` のスイートとワークスペース | 形式の変更なし。移行の作業は不要。0.11.0 に戻しても動く |
| `improve.json`・`settings.json`・`spend.json`・`spend-total.json` | 変更なし |
| プラグインとして入れた環境 | `claude plugin update wikiskill@wikiskill`（`plugin.json` の `version` が上がるので更新される） |
| `tools/integrate.py` で入れた環境 | 同じコマンドを `--force` 付きで実行し直す（`CLAUDE.md` の手順どおり） |
| Python から `wikiskill.backends` を使うコード | 名前はすべて残る。`BackendError` に `kind` などの属性が増える。未ログインと利用上限は `run_agent` が `infra_error` で返さず例外を送出する（下の「動きが変わる点」） |

**動きが変わる点（どれも失敗時だけ）**

1. 未ログイン・請求の問題・使えないモデル・受け付けられない引数は、やり直さずにすぐ止まり、原因と対処を表示する（今までは 3 回試してから「CLI の障害」）。
2. CLI の利用上限は「paused」として止まる（`improve` は終了コード 0 で、次の feedback で続きから。`evolve` / `evaluate` は終了コード 1 で、同じコマンドの再実行で続きから）。
3. 版を確認できた claude では、エージェント実行で予約・通知・ほかのセッションへの送信の道具を禁止する。上限つきの実行では 1 回の費用にも上限を付ける。
4. 失敗した呼び出しを「費用を報告しない呼び出し」として数えない。

利用者から見て壊れるもの（オプション・設定キー・ファイル形式・フォルダ構成）は無いので、**0.12.0** とする。

## 9. 段階的な計画

### 第 0 段階（この PR）

- [x] 設計書（この文書）
- [ ] `wikiskill/agent_cli/`: 版の判定、機能表、引数の組み立て、出力の解析、エラーの分類（6.2 節）
- [ ] `backends.py` をその上に載せ替え（6.3 節）。古い設定ファイルの互換をテストで固定
- [ ] `check` に版・引数の決まり方の表示を追加
- [ ] `CHANGELOG.md`、0.12.0、ドキュメントの該当箇所

### 第 1 段階: 上限の集約と、導入先での確認が要る修正（小さい PR を 3 つ）

1. **`budget.py`**（6.4 節）。移すだけで動きは変えない。テスト: 既存の `BudgetTest`・`test_improve.py` がそのまま通ること、再エクスポートの確認を 1 件足す。
2. **`tools/integrate.py --allow-skill`**（6.7 節）。フックの登録と同じ関数の形で `permissions.allow` に追記する（重複させない・ほかの設定を残す・最初の書き換えの前に `.wikiskill.bak`）。
   `Skill(name)` の書式は https://code.claude.com/docs/en/skills の「Restrict Claude's skill access」。テスト: `tests/test_hook.py` の登録のテストと同じ組み立てで、追加・再実行・`--no-hook` との組み合わせ。
   実機確認: 2.1.325 以降で、`/loop` から wikiskill を呼んで確認が出ないこと。
3. **`check` の追加表示**: `credential_expires_at` が 10 日以内なら警告。codex の利用量（`turn.completed.usage`）を台帳の `tokens_in` / `tokens_out` に足す。
   実機確認が要るもの: ログイン済みの claude 2.1.325 と codex 0.169.0 で `check` が最後まで通ること（この PR の環境では確かめられていない。4 章）。

### 第 2 段階: wiki の lint と決定的な圧縮（6.5 節 (a)(b)）

- 新規 `wikiskill/wiki_lint.py`: `lint(ws: Workspace, iteration: int) -> list[Issue]`、`compact(ws: Workspace, iteration: int, keep_impact: int = 6, keep_logs: int = 12) -> list[Path]`、
  `Issue = dataclass(check: str, page: str, detail: str)`。
- `cli.py`: `lint <workspace> [--fix]`（`--fix` は `compact` だけを行う）。
- `evolve.Evolution._maintain`: 先頭で `compact`、末尾で `lint` を呼び、`wiki/lint.json` を書く。`maintainer.build_prompt` が lint の結果を末尾に足す。
- テスト: 各検査につき「問題がある wiki」と「無い wiki」。`compact` は、(1) 直近の節が残る、(2) archive の内容と 1 行の要約が合う、(3) 2 回実行しても同じ結果、
  (4) archive を書いた直後に止まってもやり直せる、(5) メンテナのプロンプトに `archive/` が入らない。
- 互換: 0.12.0 のワークスペースでそのまま動く。0.11.0 に戻しても `wiki/archive/` と `lint.json` は無視される。

### 第 3 段階: モデルによる統合（6.5 節 (c)）

- 新規 `wikiskill/consolidator.py`（`maintainer.py` と同じ構成: `build_prompt`・`run_consolidator`・`apply_output`）と `prompts/wiki_consolidator.md`。
- `state.json` の段階に `consolidate` を足さない（maintain の中の前処理として動かし、結果を `runs/iter_NNN/consolidation.json` に残す）。段階の名前を増やすと、古い版に戻したときに再開できなくなるため。
- 評価: 付属の例（`examples/sales-analysis`）で 12 反復以上回し、統合ありとなしで (1) メンテナのプロンプトの字数、(2) val の最終スコア、(3) 採用された提案の数を比べる。
  スコアが下がるなら既定で無効にして `improve --consolidate` で有効にする。

### 第 4 段階: 大きいモジュールの分割（動きは変えない）

- `cli.py` → `cli.py`（parser と `main`）+ `commands/run.py`（check・evolve・improve・evaluate・review）+ `commands/files.py`（残り）。`cli.cmd_*` と `cli._backends` は再エクスポートを残す（テストが使う）。
- `improve.py` → `feedback.py`（記録・取り消し・取り込み）+ `autostart.py`（設定の保存・自動起動・ログ）+ `improve.py`（再エクスポートと書き出し）。
- 1 つの PR では 1 モジュールだけを分ける。テストは変更せずに通ること。

### 第 5 段階: 共有ライブラリへの置き換え

- 共有の `agent_cli` ができたら、`wikiskill/agent_cli/` をそのコピーに置き換える。`backends.py` の import 先は変わらない。
- プロセスの実行（`run_process`・`_kill_tree`）をライブラリの実装に寄せる（7 章の 6）。
- `tools/integrate.py` はパッケージをフォルダごとコピーするので、同梱の手順は変わらない。ライセンス表示を `NOTICE` に足す。

## 10. 関連する実装との比較

（この節は、この PR の中で資料を確認してから追記する。）

## 11. 参考資料

- 論文: https://arxiv.org/html/2608.27454
- Claude Code の変更履歴: https://code.claude.com/docs/en/changelog
- Claude Code のドキュメント: https://code.claude.com/docs/en/headless 、https://code.claude.com/docs/en/cli-reference 、https://code.claude.com/docs/en/permissions 、
  https://code.claude.com/docs/en/skills 、https://code.claude.com/docs/en/plugins 、https://code.claude.com/docs/en/scheduled-tasks 、https://code.claude.com/docs/en/hooks 、https://code.claude.com/docs/en/settings
- Codex CLI: https://github.com/openai/codex （`codex exec --help` 0.169.0 の出力を 4 章に記録）
- 関連する実装: https://github.com/ashutoshsinghpr7/wikiskill 、https://github.com/Huangdingcheng/SkillWiki 、https://github.com/OSU-NLP-Group/SkillWeaver
