# hdm (Hyperliquid Discord Monitor)

Hyperliquid の `userFills` を監視し、対象取引を Discord Webhook に通知する Python アプリです。

- 複数アドレスを 1 つの WebSocket 接続で監視
- SQLite による重複通知防止
- 起動直後の履歴・受信通知の抑制
- アドレスごとのタグ / 個別 Webhook 対応
- WebSocket 切断・無通信時の自動再接続
- Docker / Docker Compose 対応

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [動作要件](#%E5%8B%95%E4%BD%9C%E8%A6%81%E4%BB%B6)
- [セットアップ（ローカル実行）](#%E3%82%BB%E3%83%83%E3%83%88%E3%82%A2%E3%83%83%E3%83%97%E3%83%AD%E3%83%BC%E3%82%AB%E3%83%AB%E5%AE%9F%E8%A1%8C)
- [addresses.txt 形式](#addressestxt-%E5%BD%A2%E5%BC%8F)
- [実行方法](#%E5%AE%9F%E8%A1%8C%E6%96%B9%E6%B3%95)
  - [通常監視](#%E9%80%9A%E5%B8%B8%E7%9B%A3%E8%A6%96)
  - [バックグラウンド起動（daemon）](#%E3%83%90%E3%83%83%E3%82%AF%E3%82%B0%E3%83%A9%E3%82%A6%E3%83%B3%E3%83%89%E8%B5%B7%E5%8B%95daemon)
  - [Webhook の一時上書き](#webhook-%E3%81%AE%E4%B8%80%E6%99%82%E4%B8%8A%E6%9B%B8%E3%81%8D)
  - [テストプレビュー](#%E3%83%86%E3%82%B9%E3%83%88%E3%83%97%E3%83%AC%E3%83%93%E3%83%A5%E3%83%BC)
- [環境変数](#%E7%92%B0%E5%A2%83%E5%A4%89%E6%95%B0)
- [DB と通知仕様](#db-%E3%81%A8%E9%80%9A%E7%9F%A5%E4%BB%95%E6%A7%98)
- [Docker 実行](#docker-%E5%AE%9F%E8%A1%8C)
  - [準備](#%E6%BA%96%E5%82%99)
  - [起動](#%E8%B5%B7%E5%8B%95)
  - [ログ確認](#%E3%83%AD%E3%82%B0%E7%A2%BA%E8%AA%8D)
  - [停止](#%E5%81%9C%E6%AD%A2)
- [リリース / デプロイ方法](#%E3%83%AA%E3%83%AA%E3%83%BC%E3%82%B9--%E3%83%87%E3%83%97%E3%83%AD%E3%82%A4%E6%96%B9%E6%B3%95)
  - [デプロイのトリガー](#%E3%83%87%E3%83%97%E3%83%AD%E3%82%A4%E3%81%AE%E3%83%88%E3%83%AA%E3%82%AC%E3%83%BC)
  - [リリース手順](#%E3%83%AA%E3%83%AA%E3%83%BC%E3%82%B9%E6%89%8B%E9%A0%86)
  - [GitHub Actions の設定](#github-actions-%E3%81%AE%E8%A8%AD%E5%AE%9A)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## 動作要件

- Python 3.10 以上（Dockerfile は `python:3.14-slim` を使用）
- pip
- Discord Webhook URL
- Hyperliquid API / WebSocket に接続できるネットワーク

## セットアップ（ローカル実行）

1. 依存関係をインストールします。

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

2. `.env.example` をコピーして `.env` を作成します。

   ```bash
   cp .env.example .env
   ```

3. `.env` に Webhook と必要な設定を記載します。

   ```env
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy
   NOTIFICATION_SUPPRESSION_SECONDS=600
   DB_DIRECTORY=./data
   ```

4. 監視対象を `addresses.txt` に記載します。

   ```txt
   0x1234567890abcdef1234567890abcdef12345678
   0xabcdef1234567890abcdef1234567890abcdef12,tag-name
   0x9999999999999999999999999999999999999999,team-alpha,https://discord.com/api/webhooks/aaa/bbb
   ```

`.env` は Git にコミットしないでください。`.env.template` は GitHub Actions がデプロイ時に環境別 Secret から `.env` を生成するためのテンプレートであり、ローカル実行には `.env.example` を使用します。

## addresses.txt 形式

1 行に 1 アドレスを記載します。カンマ区切りでタグと個別 Webhook URL を指定できます。空行は無視され、アドレスは小文字に正規化されます。

- 1 列目（必須）: Hyperliquid アドレス
- 2 列目（任意）: タグ（Discord 通知タイトルに表示）
- 3 列目（任意）: そのアドレス専用の Discord Webhook URL

```text
address[,tag[,webhook_url]]
```

個別 Webhook を指定した場合も、デフォルトの `DISCORD_WEBHOOK_URL` が通知先として使用されます。個別 Webhook は追加の通知先です。

## 実行方法

エントリポイントは `hdm.py` です。ヘルプは次のコマンドで確認できます。

```bash
python hdm.py -h
python hdm.py monitor -h
python hdm.py tests -h
```

### 通常監視

```bash
python hdm.py monitor addresses.txt
```

`addresses_file` の既定値は `addresses.txt` です。`monitor` は省略可能です（後方互換）。

```bash
python hdm.py addresses.txt
```

### バックグラウンド起動（daemon）

```bash
python hdm.py monitor addresses.txt --daemon
```

daemon 起動時に次のファイルが使用されます。

- PID: `/tmp/hyperliquid_monitor_multi.pid`
- ログ: `/tmp/hyperliquid_monitor_multi.log`
- エラーログ: `/tmp/hyperliquid_monitor_multi_error.log`

停止する場合は PID ファイルのプロセスへ `SIGTERM` を送信します。

```bash
kill "$(cat /tmp/hyperliquid_monitor_multi.pid)"
```

### Webhook の一時上書き

環境変数を変更せず、コマンド単位でデフォルト Webhook を上書きできます。

```bash
python hdm.py monitor addresses.txt \
  --webhook-url "https://discord.com/api/webhooks/xxx/yyy"
```

### テストプレビュー

`tests` は WebSocket で fills を受信し、通常監視を起動せずに内容を標準出力へ表示します。デフォルトでは Discord へ送信しません。

```bash
python hdm.py tests \
  --addresses-file addresses.txt \
  --timeout-seconds 60 \
  --max-entries 10
```

Discord にも送る場合は `--post` を指定します。送信前に Webhook URL と対象アドレスを確認してください。

```bash
python hdm.py tests -f addresses.txt --post
```

`tests post` は `tests --post` と同じ後方互換の書式です。

## 環境変数

| 変数 | 必須 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | monitor では必須 | なし | デフォルト通知先の Webhook URL |
| `NOTIFICATION_SUPPRESSION_SECONDS` | 任意 | `60` | 同一アドレス・coin・direction・清算種別の通知を抑制する秒数 |
| `USER_FILL_INACTIVITY_RECONNECT_SECONDS` | 任意 | `900` | 監視対象アドレスからuserFillsを受信しない場合に再接続するまでの秒数 |
| `DB_DIRECTORY` | 任意 | `.` | SQLite DB の保存先 |
| `HEALTHCHECK_FILE` | 任意 | `/tmp/healthcheck.txt` | 通知対象の fill を処理したときに更新するファイル |
| `TEST_PREVIEW_TIMEOUT_SECONDS` | 任意 | `60` | `tests` の待機秒数 |
| `TEST_PREVIEW_MAX_ENTRIES` | 任意 | `10` | `tests` の最大表示件数 |
| `LOG_LEVEL` | 任意 | `INFO` | Python logging のログレベル（例: `DEBUG`, `INFO`, `WARNING`, `ERROR`） |

`WEBSOCKET_ACTIVITY_TIMEOUT` は後方互換のため引き続き利用できますが、`USER_FILL_INACTIVITY_RECONNECT_SECONDS` を優先してください。ログには実行コンテナのホスト名とプロセスIDを含め、複数コンテナ・プロセスのログが混在しても識別できるようにしています。DEBUG/INFO/WARNING は stdout、ERROR 以上は stderr に出力します。

## DB と通知仕様

- アドレスごとに `trades_<アドレス末尾8文字>.db` を作成します。
- 既存の fill は一意な取引 UID と tx hash 等で判定し、再通知しません。
- 起動時に DB が空の場合、Hyperliquid の `user_fills` 履歴を DB にシードします。シードした履歴は通知しません。
- WebSocket 接続後 60 秒間は通知を抑制し、再起動直後の履歴再送を防ぎます。
- 通知対象の direction は次の 4 種類です。
  - `Open Long`
  - `Close Long`
  - `Open Short`
  - `Close Short`
- Discord 通知に失敗した場合は最大 3 回まで再試行します。

`docker-compose.yml` は現在 `addresses.txt` のみを bind mount しています。コンテナの再作成後も DB を保持する必要がある場合は、運用環境で `DB_DIRECTORY` と一致する DB 用 bind mount または Docker volume を構成してください。GCE デプロイではデプロイ先の構成も含めて永続化方法を別途確認してください。

## Docker 実行

### 準備

- `.env`（`cp .env.example .env` で作成）
- `addresses.txt`
- DB を永続化する場合は `data/` と対応する volume 設定

### 起動

```bash
docker compose up -d --build
```

### ログ確認

```bash
docker compose logs -f
```

### 停止

```bash
docker compose down
```

`docker-compose.yml` はホストの `addresses.txt` を `/app/addresses.txt` に bind mount します。Dockerfile はビルド時に `.env` もイメージへコピーするため、`.env` の取り扱いとイメージの共有範囲に注意してください。

## リリース / デプロイ方法

このリポジトリは `.github/workflows/deploy.yml`（`deploy-to-gce`）から、組織共有の `deg-labs/github-workflows/.github/workflows/gce-deploy.yml@main` を呼び出し、Workload Identity Federation で認証して GCE へデプロイします。デプロイは `deg-develop` チーム所属者に制限されています。

### デプロイのトリガー

| 対象環境 | トリガー | 備考 |
| --- | --- | --- |
| `dev` | PR に `dev` ラベルを付与 | PR ラベルイベントで実行 |
| `stg` | PR に `stg` ラベルを付与 | PR ラベルイベントで実行 |
| `prd` | `main` への push | PR を `main` にマージした後に実行 |
| `dev` / `stg` / `prd` | Actions の手動実行 | `deploy-to-gce` の `Run workflow` で環境を選択 |

`prd` ラベルを PR に付与しても、現在のワークフローでは本番デプロイは実行されません。本番は `main` へのマージ後の push、または `main` ref からの手動実行を使用します。手動実行の環境選択は `dev`、`stg`、`prd` です。

### リリース手順

1. `main` から `feat/`、`fix/`、`chore/` などの作業ブランチを作成し、変更を実施します。
2. ローカルで動作確認し、`main` 向けに PR を作成します。PR 作成時には Docker Build ワークフローも実行されます。
3. 開発環境またはステージング環境へ反映する場合は、PR にそれぞれ `dev` または `stg` ラベルを付与します。
4. GCE 上の `/opt/apps/hdm` で Docker Compose が再構築され、対象環境の Webhook と `addresses.txt` が設定されます。
5. デプロイ後に Discord 通知、コンテナ状態、GitHub Actions の結果を確認します。
6. 本番リリースは確認済みの PR を `main` にマージします。`main` への push が本番デプロイを開始します。

### GitHub Actions の設定

GitHub リポジトリには、次の Repository Secrets が必要です。`DEV`、`STG`、`PRD` はデプロイ先環境の接頭辞です。

- 環境別 Secret
  - `DEV_GCP_WIF_PROVIDER` / `STG_GCP_WIF_PROVIDER` / `PRD_GCP_WIF_PROVIDER`
  - `DEV_GCP_SA_EMAIL` / `STG_GCP_SA_EMAIL` / `PRD_GCP_SA_EMAIL`
  - `DEV_GCE_INSTANCE_NAME` / `STG_GCE_INSTANCE_NAME` / `PRD_GCE_INSTANCE_NAME`
  - `DEV_DISCORD_WEBHOOK_URL` / `STG_DISCORD_WEBHOOK_URL` / `PRD_DISCORD_WEBHOOK_URL`
  - `DEV_ADDRESSES_TXT` / `STG_ADDRESSES_TXT` / `PRD_ADDRESSES_TXT`
- 共通 Secret
  - `GCE_ZONE`
  - `ORG_GH_APP_ID`
  - `ORG_GH_APP_PRIVATE_KEY`

Repository Variables には次を設定できます。

- `NOTIFICATION_SUPPRESSION_SECONDS`（未設定時はデプロイワークフロー側で `3600`）

デプロイ時には `.env.template` へ環境別 Webhook と DB 設定を `envsubst` で埋め込み、Secret から生成した `addresses.txt` とともに GCE へ転送します。生成した `.env` はソースアーカイブには含めず、GCE 上の `/opt/apps/hdm/.env` として配置されます。
