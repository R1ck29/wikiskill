# CLI の設定

WikiSkill はモデルを直接呼ばず、`claude`（Claude Code）または `codex`（Codex CLI）コマンドを実行します。
そのため **CLI が単体で動く状態なら、認証方式（API キー、Bedrock、社内ゲートウェイなど）を問わず使えます。**

## 1. 動作確認

```bash
python -m wikiskill check --backend claude --model sonnet
```

```bash
python -m wikiskill check --backend codex --model gpt-5.5
```

| 表示 | 意味と対処 |
|---|---|
| `command 'claude' -> NOT FOUND` | PATH にコマンドがない。CLI をインストールするか、設定ファイルの `command` にフルパスを書く |
| `error: claude exited 1: ... unknown option` | お使いの CLI のバージョンにない引数がある。設定ファイルで引数を変更（3 章） |
| `codex error: ... model is not supported` | その CLI の既定モデルが使えない。`--model` で使えるモデルを指定 |
| `error: sum.txt is missing or not 42: ...` | エージェントがツールを使えない。管理者の設定で `bypassPermissions` が禁止されていることが多いので、3 章の例のように許可するツールを指定する |
| `claude error: ... authentication` などの認証エラー | `claude -p "hello"` が単体で動くようにする（4 章「認証」） |
| `run_agent(): ok=True answer='42' sum.txt='42'` | 準備完了 |

`--model` を省略すると CLI 自身の既定モデルが使われます。`check` のエージェント実行は、シェルで足し算をして結果をファイルに書かせる
テストなので、ツールが禁止されている環境（暗算で答えられてしまう）も見分けられます。

## 2. 呼び出し方と既定の引数

WikiSkill が行う呼び出しは 2 種類です。

- **テキスト生成**（Wiki メンテナ、スキル提案者、llm_judge 採点）: ツールなしで 1 回答える
- **エージェント実行**（推論エージェント）: 一時作業フォルダの中でツールを使ってタスクを解く

プロンプトは標準入力、長いシステムプロンプトは一時ファイルで渡し、コマンドラインには短い引数だけを置きます
（Windows のコマンド長制限と `.cmd` 経由での引用符崩れを避けるため）。既定の引数は `claude 2.1.295` と
`codex-cli 0.146.1` で動作を確認した値です（`wikiskill/backends.py` の `DEFAULTS`）。

| キー | claude | codex |
|---|---|---|
| `complete_args`（テキスト生成） | `-p --safe-mode --output-format json --tools "" --no-session-persistence` | `exec --json --skip-git-repo-check --ephemeral --sandbox read-only -c web_search=disabled` |
| `agent_args`（エージェント実行） | `-p --safe-mode --output-format stream-json --verbose --permission-mode bypassPermissions --disallowedTools WebFetch,WebSearch --no-session-persistence` | `exec --json --skip-git-repo-check --ephemeral --sandbox workspace-write -c web_search=disabled` |

これらに加えてプログラムが付け足すもの:

- モデル: claude は `--model`、codex は `-m`
- システムプロンプト: claude はテキスト生成で `--system-prompt-file`、エージェント実行で `--append-system-prompt-file`
  （Claude Code 本来のエージェント用プロンプトを残して追記）。codex にはこの指定がないため、プロンプトの先頭に付けます
- codex の作業フォルダ `-C <フォルダ>` と、標準入力を読む `-`

上限つきの実行（`improve --daily-budget`、自動の実行は既定 10 ドル）は、claude の出力にある `total_cost_usd` を費用として数えます。
`--output-format` を変えてこの値が出なくなると費用を数えられず上限が効かなくなるので、`complete_args` / `agent_args` を変えるときは JSON 出力のままにしてください。
codex は費用を報告しないため、上限つきの実行を 1 日 1 回にします。

各引数の意図:

| 引数 | 意図 |
|---|---|
| `--safe-mode`（claude） | 個人の CLAUDE.md・スキル・MCP サーバー・フックを読み込まない（実験の再現性と、トークン消費の削減。認証とモデル選択は通常どおり） |
| `--disallowedTools WebFetch,WebSearch` / `-c web_search=disabled` | Web 検索を使わせない |
| `--permission-mode bypassPermissions` / `--sandbox workspace-write` | 無人で実行できるようにする（作業は毎回新しい一時フォルダ内） |

## 3. 設定ファイルで変更する

JSON ファイルを作り、`--backend-config` で指定します。書いたキーだけが既定値を上書きします。
`complete_args` / `agent_args` は**丸ごと置き換え**なので、残したい既定の引数も書いてください。
次の例は、社内ポリシーで `bypassPermissions` が使えない場合に、許可するツールを限定する設定です。

```json
{
  "claude": {
    "command": "C:\\Tools\\claude.cmd",
    "agent_args": ["-p", "--safe-mode", "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Write,Edit",
                   "--disallowedTools", "WebFetch,WebSearch", "--no-session-persistence"],
    "timeout": 3600
  },
  "codex": {"retries": 1}
}
```

```bash
python -m wikiskill evolve my-suite --workspace workspaces/my --backend-config backend.json
```

| キー | 既定 | 意味 |
|---|---|---|
| `command` | `claude` / `codex` | 実行するコマンド（フルパス可） |
| `model_flag` | `--model` / `-m` | モデル指定に使う引数名 |
| `complete_args` / `agent_args` | 2 章の表 | 引数の一覧（置き換え） |
| `timeout` | 1800 | 1 回の呼び出しの制限秒数。超えると CLI とその子プロセスをまとめて停止 |
| `retries` | 2 | CLI が失敗したときの再試行回数 |
| `retry_wait` | 10 | 再試行までの待ち秒数（n 回目は n 倍） |

## 4. 社内環境での注意

- **認証**: WikiSkill は認証情報を扱わず、CLI がいつもどおり認証します（`--safe-mode` でも認証は通常どおり）。API キーなら
  `ANTHROPIC_API_KEY`（または Claude Code 設定の `apiKeyHelper`）、Bedrock・Vertex・社内ゲートウェイならその環境変数を、
  `claude` を実行する環境で設定してください。`feedback` が裏で起動する自動の改善は、記録した Claude Code のセッションの環境変数を引き継ぎます。
- **自動の改善のモデル**: claude では別名 `sonnet` を渡します。Bedrock や社内ゲートウェイでも CLI がその環境の Sonnet に解決しますが、
  別名が使えない環境では `ANTHROPIC_DEFAULT_SONNET_MODEL` でその環境のモデル名を指定してください。
- **管理者の設定（managed settings）**: `--safe-mode` でも管理者の設定は効きます。`bypassPermissions` が禁止されていると
  エージェント実行が失敗するので、3 章の例のように `acceptEdits` と許可するツールを指定してください（`check` で確かめられます）。
- **評価を知らせるフック**: `tools/integrate.py` が登録します。登録先は、グローバル設定なら `~/.claude/settings.json`、
  プロジェクト（またはプロジェクトの `.claude/skills`）なら個人用の `.claude/settings.local.json`（共有の settings.json に
  この PC のパスを書かないため）、プラグインなら `hooks/hooks.json` です。ほかの設定やフックはそのまま残し
  （変更が無ければファイルに触れない。シンボリックリンクはリンク先を書き換える）、最初の書き換えの前に `<設定ファイル>.wikiskill.bak` を作ります。
  フックは sh / bash（Windows は Git Bash）で動く前提で、フックの実行に PowerShell を指定した Windows では動きません。コマンドは、組み込みを実行した Python のフルパスです。
  その Python を消したり入れ替えたりしたら、組み込みをやり直してください。管理者の設定の `allowManagedHooksOnly` や `disableAllHooks` で
  フックが止められている環境では、評価にはスキルの説明文だけで気づくことになります（組み込み時に警告が出ます）。
  手で入れる場合は、`hooks.UserPromptSubmit` に `"<python>" "<スキル>/scripts/judgement_hook.py"` を実行する command フックを足します。
- **ネットワークドライブ**: スイートとワークスペースはローカルのドライブに置いてください。ファイルロックが使えないドライブでは
  `cannot lock ... keep WikiSkill folders on a local drive` と表示して止まります。
- **WikiSkill 自身の通信**: ありません（ネットワーク系モジュールを使っていないことを `tests/test_no_network.py` で確認しています）。
  通信は CLI からモデルへの呼び出しだけです。エージェントがシェル経由で外部に接続できるかどうかは、CLI と社内ネットワークの設定によります。
- **`--safe-mode` が使えない CLI**: 古い claude では `unknown option` になります。`complete_args` / `agent_args` から外してください。
- **Windows での自動の改善**: 隠れたコンソールで動くため、ウィンドウは開きません。止めたいときはタスクマネージャーで
  `python` を終了するか、スイートの `improve.json` を消して次から起動しないようにします。
- **Codex の個人設定**: `codex exec` は `~/.codex` 配下の個人設定（インストール済みスキルなど）を読み込みます
  （codex-cli 0.146.1 で確認）。`--safe-mode` に当たる指定は使っていないため、実験を純粋にしたい場合は Codex 側で無効化してください。
- **CLI の障害**: タイムアウトや API エラーは自動で再試行し、それでも失敗したタスクは**採点せずに**処理を止めます
  （障害を「不正解」として wiki や採否判定に混ぜないため）。同じコマンドを再実行すると、失敗したタスクだけをやり直します。
- **データの保存先**: 実行記録・作業フォルダのコピー・レビューシートは、`evolve` ではワークスペース、`evaluate` と `review` では
  `--out` のフォルダに保存されます。業務データを含むため、共有フォルダや Git に置かないでください。
