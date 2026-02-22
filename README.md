# hdm (Hyperliquid Discord Monitor)

Hyperliquid の `userFills` を監視し、対象取引を Discord Webhook に通知する Python アプリです。

- 複数アドレス監視
- SQLite による重複通知防止
- 起動直後の履歴通知抑制
- アドレスごとのタグ / 個別 Webhook 対応
- Docker / Docker Compose 対応

## 動作要件

- Python 3.10+（Dockerfile は `python:3.10-slim` を使用）
- pip
- Discord Webhook URL

## セットアップ（ローカル実行）

1. 依存関係をインストール

```bash
pip install -r requirements.txt
```

2. `.env` を作成（`.env.example` をコピー）

```bash
cp .env.example .env
```

3. `.env` に Webhook 等を設定

```env
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
NOTIFICATION_SUPPRESSION_SECONDS=600
DB_DIRECTORY=./data
```

4. `addresses.txt` を用意

```txt
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12,tag-name
0x9999999999999999999999999999999999999999,team-alpha,https://discord.com/api/webhooks/aaa/bbb
```

## addresses.txt 形式

1 行に 1 アドレス。カンマ区切りで以下を指定できます。

- 1 列目（必須）: アドレス
- 2 列目（任意）: タグ（通知タイトルに表示）
- 3 列目（任意）: そのアドレス専用 Webhook URL

形式:

```txt
address[,tag[,webhook_url]]
```

## 実行方法

エントリポイントは `hdm.py` です。

### Usage

```bash
$ python3 hdm.py -h
Database directory set to: ./data
usage: hdm.py [-h] command ...

Hyperliquid Trade Monitor (Multi-Address WebSocket Support)

positional arguments:
  command
    monitor   Run trade monitor
    tests     Preview recent fills without running full monitor

options:
  -h, --help  show this help message and exit
```

```bash
$ python3 hdm.py monitor -h
Database directory set to: ./data
usage: hdm.py monitor [-h] [-d] [--background] [--webhook-url WEBHOOK_URL] [addresses_file]

positional arguments:
  addresses_file        Path to addresses file (default: addresses.txt)

options:
  -h, --help            show this help message and exit
  -d, --daemon          Run as background daemon (single process monitoring all addresses) (default: False)
  --background          Internal flag used by daemon child process (default: False)
  --webhook-url WEBHOOK_URL
                        Override DISCORD_WEBHOOK_URL environment variable (default: None)
```

```bash
$ python3 hdm.py tests -h
Database directory set to: ./data
usage: hdm.py tests [-h] [-f ADDRESSES_FILE] [--post] [--timeout-seconds TIMEOUT_SECONDS] [--max-entries MAX_ENTRIES] [{post}]

positional arguments:
  {post}                Legacy positional mode (equivalent to --post) (default: None)

options:
  -h, --help            show this help message and exit
  -f, --addresses-file ADDRESSES_FILE
                        Path to addresses file used in preview (default: addresses.txt)
  --post                Also post preview messages to Discord (default: False)
  --timeout-seconds TIMEOUT_SECONDS
                        Seconds to wait before stopping preview (default: 60)
  --max-entries MAX_ENTRIES
                        Maximum fills to print before stopping preview (default: 10)
```

### 通常監視

```bash
python hdm.py monitor addresses.txt
```

`monitor` は省略可能です（後方互換）。

```bash
python hdm.py addresses.txt
```

### バックグラウンド起動（daemon）

```bash
python hdm.py monitor addresses.txt --daemon
```

- PID: `/tmp/hyperliquid_monitor_multi.pid`
- log: `/tmp/hyperliquid_monitor_multi.log`
- error log: `/tmp/hyperliquid_monitor_multi_error.log`

### Webhook の一時上書き

```bash
python hdm.py monitor addresses.txt --webhook-url "https://discord.com/api/webhooks/xxx/yyy"
```

### テストプレビュー

WebSocket で fills を受信し、Discord 送信せずに内容を標準出力へ表示します。

```bash
python hdm.py tests -f addresses.txt --timeout-seconds 60 --max-entries 10
```

Discord にも送る場合:

```bash
python hdm.py tests -f addresses.txt --post
```

## 環境変数

- `DISCORD_WEBHOOK_URL`（必須）: デフォルト通知先
- `NOTIFICATION_SUPPRESSION_SECONDS`（任意, default: `60`）: 同一 `(address, coin, direction)` 通知の抑制秒数
- `WEBSOCKET_ACTIVITY_TIMEOUT`（任意, default: `900`）: fill 未受信時の再接続判定秒数
- `DB_DIRECTORY`（任意, default: `.`）: SQLite DB 保存先
- `HEALTHCHECK_FILE`（任意, default: `/tmp/healthcheck.txt`）: ヘルスチェック更新対象ファイル
- `TEST_PREVIEW_TIMEOUT_SECONDS`（任意, default: `60`）: `tests` の待機秒数
- `TEST_PREVIEW_MAX_ENTRIES`（任意, default: `10`）: `tests` の最大表示件数

## DB と通知仕様

- アドレスごとに `trades_<address末尾8文字>.db` を作成
- 既存 tx hash は再通知しない
- 初回起動時、DB が空なら `user_fills` 履歴を DB にシード（通知なし）
- 起動後 60 秒は履歴由来の通知を抑制
- 通知対象 direction は次のみ:
  - `Open Long`
  - `Close Long`
  - `Open Short`
  - `Close Short`

## Docker 実行

### 1. 準備

- `.env`
- `addresses.txt`
- （任意）`data/` ディレクトリ

### 2. 起動

```bash
docker compose up -d --build
```

### 3. ログ確認

```bash
docker compose logs -f
```

### 4. 停止

```bash
docker compose down
```

`docker-compose.yml` では `addresses.txt` を bind mount しています。`DB_DIRECTORY` を `./data` などに設定して永続化してください。

## GitHub Actions での GCE デプロイ

`.github/workflows/deploy.yml` は PR に `dev` / `stg` / `prd` ラベルを付与したときに起動します。
このデプロイは開発部メンバーのみを許可しています。

必要な設定:

- Repository Secrets
  - `DEV/STG/PRD_GCP_WIF_PROVIDER`
  - `DEV/STG/PRD_GCP_SA_EMAIL`
  - `DEV/STG/PRD_GCE_INSTANCE_NAME`
  - `DEV/STG/PRD_DISCORD_WEBHOOK_URL`
  - `DEV/STG/PRD_ADDRESSES_TXT`
  - `GCE_ZONE`
- Repository Variables
  - `NOTIFICATION_SUPPRESSION_SECONDS`（未設定時 `3600`）

デプロイ先は `/opt/apps/hdm` を想定しています。
