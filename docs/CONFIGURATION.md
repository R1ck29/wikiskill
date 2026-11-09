# CLI の設定

WikiSkill はモデルを直接呼ばず、`claude`（Claude Code）または `codex`（Codex CLI）コマンドを実行します。
そのため **CLI が単体で動く状態なら、認証方式（API キー、Bedrock、LLM ゲートウェイなど）を問わず使えます。**

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
（費用を報告しない codex の扱いは README の「費用と API キー」）

各引数の意図:

| 引数 | 意図 |
|---|---|
| `--safe-mode`（claude） | 個人の CLAUDE.md・スキル・MCP サーバー・フックを読み込まない（実験の再現性と、トークン消費の削減。認証とモデル選択は通常どおり） |
| `--disallowedTools WebFetch,WebSearch` / `-c web_search=disabled` | Web 検索を使わせない |
| `--permission-mode bypassPermissions` / `--sandbox workspace-write` | 無人実行用。新しい一時フォルダは作業ディレクトリであり、WikiSkill のサンドボックスではない。claude の `bypassPermissions` は確認なしでツール・コマンドを実行でき、フォルダ外の読み書きや Bash 経由のネットワーク接続も可能（CLI・OS・組織のポリシーによる制約は別）。codex には CLI 自身の `workspace-write` が適用される。子プロセスは API キーを含む呼び出し元の環境変数を継承する |

### 既定の権限と環境変数について

機能を制限しないため、claude のエージェント実行は意図的に `bypassPermissions` を既定とし、
CLI の子プロセスは呼び出し元の環境変数（API キーを含む）を継承します。
この方針は社内利用向けにメンテナがレビューし、受け入れています。自動改善の起動時も環境を引き継ぎ、
Python の起動用に `PYTHONPATH` と `PYTHONIOENCODING` を設定します。

WikiSkill 自身はネットワーク通信を行わず、モデルへのデータ送信は `claude` / `codex` CLI に設定された
モデルプロバイダ経由です。ただし、エージェント自身の Bash などからは別のホストへ接続できるため、
通信先全体をモデルプロバイダだけに限定する仕組みではありません。
3 章のツールを限定する設定例は、組織のポリシーで必要な場合に使ってください（機能が制限される場合があります）。
評価や実行には、内容を信頼できるスイートと設定だけを使うことをお勧めします。

## 3. 設定ファイルで変更する

JSON ファイルを作り、`--backend-config` で指定します。書いたキーだけが既定値を上書きします。
`complete_args` / `agent_args` は**丸ごと置き換え**なので、残したい既定の引数も書いてください。
次の例は、組織のポリシーで `bypassPermissions` が使えない場合に、許可するツールを限定する設定です。

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

## 4. 導入先の環境での注意

- **認証**: WikiSkill は認証情報を扱わず、CLI がいつもどおり認証します（`--safe-mode` でも認証は通常どおり）。API キーなら
  `ANTHROPIC_API_KEY`（または Claude Code 設定の `apiKeyHelper`）、Bedrock・Vertex・LLM ゲートウェイならその環境変数を、
  `claude` を実行する環境で設定してください。`feedback` が裏で起動する自動の改善は、記録した Claude Code のセッションの環境変数を引き継ぎます。
- **自動の改善のモデル**: claude では別名 `sonnet` を渡します。Bedrock や LLM ゲートウェイでも CLI がその環境の Sonnet に解決しますが、
  別名が使えない環境では `ANTHROPIC_DEFAULT_SONNET_MODEL` でその環境のモデル名を指定してください。
- **管理者の設定（managed settings）**: `--safe-mode` でも管理者の設定は効きます。`bypassPermissions` が禁止されていると
  エージェント実行が失敗するので、3 章の例のように `acceptEdits` と許可するツールを指定してください（`check` で確かめられます）。
- **評価を知らせるフック**: `tools/integrate.py` が登録します。登録先は、グローバル設定なら `~/.claude/settings.json`、
  プロジェクト（またはプロジェクトの `.claude/skills`）なら個人用の `.claude/settings.local.json`（共有の settings.json に
  この PC のパスを書かないため）、プラグインなら `hooks/hooks.json` です。ほかの設定やフックはそのまま残し
  （変更が無ければファイルに触れない。シンボリックリンクはリンク先を書き換える）、最初の書き換えの前に `<設定ファイル>.wikiskill.bak` を作ります。
  フックは sh / bash（Windows は Git Bash）で動く前提で、フックの実行に PowerShell を指定した Windows では動きません。コマンドの Python は、グローバル設定とプロジェクトでは組み込みを実行した Python のフルパス、プラグインでは `python3`（Windows は `py`）です。
  その Python を消したり入れ替えたりしたら、組み込みをやり直してください。管理者の設定の `allowManagedHooksOnly` や `disableAllHooks` で
  フックが止められている環境では、評価にはスキルの説明文だけで気づくことになります（組み込み時に警告が出ます）。
  手で入れる場合は、`hooks.UserPromptSubmit` に `"<python>" "<スキル>/scripts/judgement_hook.py"` を実行する command フックを足します。
- **ネットワークドライブ**: スイートとワークスペースはローカルのドライブに置いてください。ファイルロックが使えないドライブでは
  `cannot lock ... keep WikiSkill folders on a local drive` と表示して止まります。
- **WikiSkill 自身の通信**: ありません（ネットワーク系モジュールを使っていないことを `tests/test_no_network.py` で確認しています）。
  モデルへの通信は CLI 経由です。エージェントがシェル経由で外部に接続できるかどうかは、CLI とネットワークの設定によります。
- **`--safe-mode` が使えない CLI**: 古い claude では `unknown option` になります。`complete_args` / `agent_args` から外してください。
- **Windows での自動の改善**: 隠れたコンソールで動くため、ウィンドウは開きません。止めたいときはタスクマネージャーで
  `python` を終了するか、スイートの `improve.json` を消して次から起動しないようにします。
- **Codex の個人設定**: `codex exec` は `~/.codex` 配下の個人設定（インストール済みスキルなど）を読み込みます
  （codex-cli 0.146.1 で確認）。`--safe-mode` に当たる指定は使っていないため、実験を純粋にしたい場合は Codex 側で無効化してください。
- **CLI の障害**: タイムアウトや API エラーは自動で再試行し、それでも失敗したタスクは**採点せずに**処理を止めます
  （障害を「不正解」として wiki や採否判定に混ぜないため）。同じコマンドを再実行すると、失敗したタスクだけをやり直します。
- **データの保存先**: 実行記録・作業フォルダのコピー・レビューシートは、`evolve` ではワークスペース、`evaluate` と `review` では
  `--out` のフォルダに保存されます。業務データを含むため、共有フォルダや Git に置かないでください。

## 5. 導入先で確かめること

開発中に実際のモデル呼び出しと Claude Code で動きを確かめたのは、macOS とサブスクリプションのログインだけです。
API キー・Bedrock・LLM ゲートウェイでの認証、Windows（Git Bash）、管理者の設定がある環境での動きは、導入先でしか確かめられません。
組み込んだ後に、次の順で確かめてください。

1. **CLI**: `claude -p "hello"` が単体で返答する。返答しなければ CLI の認証から直す（4 章「認証」）。
2. **認証とツールの許可**: スキルフォルダの `scripts/run_wikiskill.py check` を実行し（モデルを 2 回だけ呼ぶ）、
   `run_agent(): ok=True` まで出る。出なければ 1 章の表に従って直す。
3. **フック**: 組み込みの表示に `hook: UserPromptSubmit added to ...` が出ている。新しい会話で集計や翻訳など何か結果を出してもらい、
   「違う」と返して、Claude が記録する（初めての業務なら「覚えますか？」と聞く）。聞かなければ、組み込みの `warning:` の内容と、
   フックの実行に PowerShell を指定していないかを確かめる（4 章「評価を知らせるフック」）。
4. **自動の改善**: 評価がたまって自動の改善が初めて動いたら、`budget` で費用が数えられていること、`<ワークスペース>/background.log` に
   モデルのエラーが無いことを見る。別名 `sonnet` が使えないエラーなら `ANTHROPIC_DEFAULT_SONNET_MODEL` を設定する（4 章「自動の改善のモデル」）。
5. **置き場所**: ホームフォルダがネットワークドライブで `cannot lock` と出たら、作業フォルダをローカルのドライブに移す（4 章「ネットワークドライブ」）。
