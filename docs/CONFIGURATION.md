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
| `run_agent(): ok=True answer='42'` | 準備完了 |

`--model` を省略すると CLI 自身の既定モデルが使われます。

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

- **WikiSkill 自身の通信**: ありません（ネットワーク系モジュールを使っていないことを `tests/test_no_network.py` で確認しています）。
  通信は CLI からモデルへの呼び出しだけです。エージェントがシェル経由で外部に接続できるかどうかは、CLI と社内ネットワークの設定によります。
- **`--safe-mode` が使えない CLI**: 古い claude では `unknown option` になります。`complete_args` / `agent_args` から外してください。
- **Codex の個人設定**: `codex exec` は `~/.codex` 配下の個人設定（インストール済みスキルなど）を読み込みます
  （codex-cli 0.146.1 で確認）。`--safe-mode` に当たる指定は使っていないため、実験を純粋にしたい場合は Codex 側で無効化してください。
- **CLI の障害**: タイムアウトや API エラーは自動で再試行し、それでも失敗したタスクは**採点せずに**処理を止めます
  （障害を「不正解」として wiki や採否判定に混ぜないため）。同じコマンドを再実行すると、失敗したタスクだけをやり直します。
- **データの保存先**: 実行記録・作業フォルダのコピー・レビューシートは、`evolve` ではワークスペース、`evaluate` と `review` では
  `--out` のフォルダに保存されます。業務データを含むため、共有フォルダや Git に置かないでください。
