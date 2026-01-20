# hyperliquid-discord-monitor
## インストール

### 前提条件
- Python 3.7+
- pip パッケージマネージャー

### インストール手順

1. プロジェクトを取得（clone または download）
2. 依存関係をインストール:
```bash
pip install -r requirements.txt
```

3. プロジェクト直下に `.env` を作成:
```bash
touch .env
```

4. `.env` に Discord Webhook URL を設定:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook/url
```

5. 監視対象アドレスを記載した `addresses.txt` を作成（1行に1つ）:
```bash
touch addresses.txt
```

## Docker で実行（推奨）

Docker を使うと依存関係やプロセス管理を自動化できるため推奨です。

### 1. 前提

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

### 2. 設定

プロジェクト直下に以下の 3 点を用意します。

#### a) `addresses.txt` ファイル

監視する Hyperliquid アドレスを 1 行に 1 つ記述します。

**`addresses.txt` の例:**
```
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
```

#### b) `data` ディレクトリ

取引履歴のデータベース（SQLite）を永続化するために使用します。コンテナ再起動時のデータ消失を防ぎます。

以下のコマンドで作成します:
```bash
mkdir data
```

#### c) `.env` ファイル

アプリで必要な環境変数を定義します。

- `DISCORD_WEBHOOK_URL`: **(必須)** Discord Webhook URL
- `DB_DIRECTORY`: **(Docker では必須)** 永続ボリュームに保存するため `/app/data` を指定
- `NOTIFICATION_SUPPRESSION_SECONDS`: (任意) 同種の取引通知のクールダウン秒数（デフォルト `60`）

**`.env` の例:**
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_webhook_id/your_webhook_token
NOTIFICATION_SUPPRESSION_SECONDS=300
DB_DIRECTORY=/app/data
```

### 3. サービスの起動

設定が完了したら、以下のコマンドで管理します。

- **バックグラウンドで起動:**
  ```bash
  docker-compose up -d
  ```

- **ログをリアルタイム表示:**
  ```bash
  docker-compose logs -f
  ```
  *(`Ctrl+C` でログ表示を終了しますが、サービスは継続します)*

- **停止:**
  ```bash
  docker-compose down
  ```

---

## 使い方

### 基本
デフォルトの `addresses.txt` を監視:
```bash
python hyperliquid-discord-monitor.py addresses.txt
```

任意のファイルを指定:
```bash
python hyperliquid-discord-monitor.py custom_addresses.txt
```

### デーモンモード
バックグラウンドで実行:
```bash
python hyperliquid-discord-monitor.py addresses.txt -d
```

任意ファイル指定:
```bash
python hyperliquid-discord-monitor.py custom_addresses.txt -d
```

## 例

### セットアップ例

1. 環境変数ファイルを作成:
```bash
echo "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123456789/abcdefg" > .env
```

2. 監視するアドレスを追加:
```bash
cat > addresses.txt << EOF
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
0x9876543210fedcba9876543210fedcba98765432
EOF
```

python hyperliquid-discord-monitor.py addresses.txt

### Discord メッセージ例
取引を検知すると以下のような通知が届きます:
```
**[2024-01-15 14:30:25] New FILL**
Address: https://hypurrscan.io/address/0x1234567890abcdef1234567890abcdef12345678
Trade Tx hash: https://hypurrscan.io/tx/0xabcdef...

Coin: ETH
Price: 2450.50
Direction: Long
PnL: 🟢 125.75
Hash: 0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab
```

### ファイル構成
```
project/
├── hyperliquid-discord-monitor.py
├── .env
├── addresses.txt
├── trades.db (自動生成)
└── README.md
```

### 環境変数
- `DISCORD_WEBHOOK_URL`: Discord webhook URL（必須）

### アドレスファイル形式
`addresses.txt` は 1 行に 1 つの Ethereum アドレスを記載します:
```
0x1234567890abcdef1234567890abcdef12345678
0xabcdef1234567890abcdef1234567890abcdef12
0x9876543210fedcba9876543210fedcba98765432
```

空行は無視されるため、可読性のために空行を挟んでも問題ありません。

### デーモン化の推奨
プロセスを直接デーモン化するとスリープ状態に入る場合があります。
安定した運用のため、Supervisord の利用を推奨します。

例:
```bash
$ cat /etc/supervisor/conf.d/hyperliquid-discord-monitor.conf
[program:hyperliquid-discord-monitor]
command=python3 hyperliquid-discord-monitor.py addresses
user=darkstar
directory=/home/$USER/git/hyperliquid-discord-monitor
autostart=true
autorestart=true
stderr_logfile=/var/log/h-monitor.log
stderr_logfile_maxbytes=1MB
stdout_logfile=/var/log/h-monitor.out.log
stdout_logfile_maxbytes=1MB
stdout_logfile_backups=0
stderr_logfile_backups=0
environment=PATH="/home/$USER/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/home/$USER/.$USER/bin:/home/$USER/.cargo/bin:/home/$USER/.npm-global/bin",PYTHONPATH="/home/$USER/.local/lib/python3.11/site-packages",HOME="/home/$USER"
```
```bash
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start hyperliquid-discord-monitor
```

---

## GitHub Actions で GCE へデプロイ

このプロジェクトには GitHub Actions ワークフロー（`.github/workflows/deploy.yml`）が含まれており、Workload Identity Federation を使って Google Compute Engine (GCE) へ自動デプロイします。利用には GitHub Secrets と Variables の設定が必要です（`Settings > Secrets and variables > Actions`）。
  
参考として以下のPRのようにPR作成しラベル付与でデプロイをトリガーできます:
https://github.com/deg-labs/hdm/pull/3  

### GitHub Secrets（Repository Secrets）

以下は環境別（dev/stg/prd）に設定します:

*   **`DEV_GCP_WIF_PROVIDER` / `STG_GCP_WIF_PROVIDER` / `PRD_GCP_WIF_PROVIDER`**: Workload Identity Federation の provider リソース名
*   **`DEV_GCP_SA_EMAIL` / `STG_GCP_SA_EMAIL` / `PRD_GCP_SA_EMAIL`**: GitHub Actions が使用する Service Account のメールアドレス
*   **`DEV_GCE_INSTANCE_NAME` / `STG_GCE_INSTANCE_NAME` / `PRD_GCE_INSTANCE_NAME`**: デプロイ対象の GCE インスタンス名
*   **`DEV_DISCORD_WEBHOOK_URL` / `STG_DISCORD_WEBHOOK_URL` / `PRD_DISCORD_WEBHOOK_URL`**: Discord Webhook URL
*   **`DEV_ADDRESSES_TXT` / `STG_ADDRESSES_TXT` / `PRD_ADDRESSES_TXT`**: `addresses.txt` の内容（複数行）

以下は共通で設定します:

*   **`GCE_ZONE`**: GCE インスタンスのゾーン（例: `asia-northeast1-b`）

### GitHub Variables（Repository Variables）

非機密の設定値です:

*   **`NOTIFICATION_SUPPRESSION_SECONDS`**: (任意) 同種通知のクールダウン秒数。未設定時は `600`

**注意:** デプロイ先は `/opt/apps/hdm` を想定しています。ディレクトリが存在しない場合は作成されます。必要に応じて権限設定（`sudo`）を確認してください。
