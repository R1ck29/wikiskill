# CLI バックエンドの設定

WikiSkill はモデルを直接呼ばず、`claude`（Claude Code）または `codex`（Codex CLI）コマンドを実行します。
そのため、**CLI が単体で動く状態なら、認証方式（API キー、Bedrock、社内ゲートウェイなど）を問わず使えます。**

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
| `error: claude exited 1: ... unknown option` | お使いの CLI バージョンにない引数がある。下の設定ファイルで引数を変更 |
| `codex error: ... model is not supported` | 既定モデルが使えない。`--model` で使えるモデルを指定 |
| `run_agent(): ok=True answer='42'` | 準備完了 |

`--model` を省略すると CLI 自身の既定モデルが使われます。

## 2. 既定の引数

`claude 2.1.295` と `codex-cli 0.146.1` で動作を確認した値です（`wikiskill/backends.py` の `DEFAULTS`）。

| キー | claude | codex |
|---|---|---|
| `complete_args`（テキスト生成） | `-p --safe-mode --output-format json --tools "" --no-session-persistence` | `exec --json --skip-git-repo-check --ephemeral --sandbox read-only -c web_search=disabled` |
| `agent_args`（エージェント実行） | `-p --safe-mode --output-format stream-json --verbose --permission-mode bypassPermissions --disallowedTools WebFetch,WebSearch --no-session-persistence` | `exec --json --skip-git-repo-check --ephemeral --sandbox workspace-write -c web_search=disabled` |

プログラムが自動で付け足すもの: モデル（claude `--model` / codex `-m`）、システムプロンプトのファイル
（claude `--system-prompt-file` / `--append-system-prompt-file`）、codex の `-C <作業フォルダ>` と `-`（標準入力）。

## 3. 設定ファイルで変更する

JSON ファイルを作り、`--backend-config` で指定します。書いたキーだけが既定値を上書きします。

```json
{
  "claude": {
    "command": "C:\\Tools\\claude.cmd",
    "agent_args": ["-p", "--output-format", "stream-json", "--verbose",
                   "--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Write,Edit",
                   "--no-session-persistence"],
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
| `complete_args` / `agent_args` | 上表 | 引数の一覧（**置き換え**。追加ではない） |
| `timeout` | 1800 | 1 回の呼び出しの制限秒数。超えると子プロセスごと停止 |
| `retries` | 2 | CLI 障害時の再試行回数 |
| `retry_wait` | 10 | 再試行の待ち秒数（回数に比例して延びる） |

## 4. 社内環境での注意

- **WikiSkill 自身の通信**: ありません（ネットワーク系モジュールを使っていないことをテスト `tests/test_no_network.py` で確認）。
  通信は CLI からモデルへの呼び出しだけです。
- **エージェントの権限**: 推論エージェントは一時フォルダ内で、Claude は `bypassPermissions`（確認なしでコマンド実行）、
  Codex は `workspace-write` サンドボックスで動きます。社内ポリシーで `bypassPermissions` が禁止されている場合は、
  上の例のように `acceptEdits` と `--allowedTools` に変えてください。Web 検索ツールは既定で無効です。
  なお、エージェントがシェル経由で外部に接続できるかどうかは CLI と社内ネットワークの設定に依存します。
- **`--safe-mode`**: 個人の CLAUDE.md・スキル・MCP サーバー・フックを読み込まない指定です（認証とモデル選択は通常どおり）。
  お使いの claude が対応していなければ `complete_args` / `agent_args` から外してください。
- **Codex の個人設定**: `codex exec` は `~/.codex` 配下の個人設定（インストール済みスキルなど）を読み込みます
  （codex-cli 0.146.1 で確認）。Claude の `--safe-mode` に相当する指定は使っていないため、実験を純粋にしたい場合は
  Codex 側の設定で無効化してください。
- **データの保存先**: 実行記録・作業フォルダのコピー・レビューシートはすべてワークスペース内に保存されます。
  業務データを含むため、共有フォルダや Git に置かないでください。
