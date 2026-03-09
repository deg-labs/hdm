import sys
import os
import time
import signal
import atexit
import argparse
import subprocess
import asyncio
import threading
from websocket._exceptions import WebSocketConnectionClosedException
from datetime import datetime, timezone
from collections import defaultdict
import requests
import json
import sqlite3
from dotenv import load_dotenv
from hyperliquid.info import Info

from .addresses import load_addresses
from .models import Trade
from .ws_monitor import HyperliquidUserFillsMonitor

load_dotenv()

# .envから各種設定を読み込む
NOTIFICATION_SUPPRESSION_SECONDS = int(os.getenv('NOTIFICATION_SUPPRESSION_SECONDS', 60))
WEBSOCKET_ACTIVITY_TIMEOUT = int(os.getenv('WEBSOCKET_ACTIVITY_TIMEOUT', 900)) # 15分
DB_DIRECTORY = os.getenv('DB_DIRECTORY', '.') # デフォルトはカレントディレクトリ
HEALTHCHECK_FILE = os.getenv('HEALTHCHECK_FILE', '/tmp/healthcheck.txt')

# DB保存ディレクトリが存在しない場合は作成
if DB_DIRECTORY != '.':
    os.makedirs(DB_DIRECTORY, exist_ok=True)
    print(f"Database directory set to: {DB_DIRECTORY}")

last_notification_time = defaultdict(float)

trade_cache = defaultdict(list)
monitor_instances = {}
main_loop = None
monitor_tasks = {}
collateral_ticker_cache = {}
spot_meta_index_cache = None

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_REQUEST_TIMEOUT = 10

processed_trades = set()
startup_grace_period = {}

def make_trade_uid(trade: Trade) -> str:
    timestamp = trade.timestamp.isoformat() if trade.timestamp else ""
    return "|".join([
        trade.address or "",
        trade.tx_hash or "",
        trade.coin or "",
        trade.direction or "",
        timestamp,
        f"{trade.price}",
        f"{trade.size}",
    ])

def touch_healthcheck_file():
    """ヘルスチェックファイルをtouch"""
    try:
        with open(HEALTHCHECK_FILE, 'a'):
            os.utime(HEALTHCHECK_FILE, None)
    except Exception as e:
        sys.stderr.write(f"Failed to touch healthcheck file: {e}\n")

def send_to_discord(webhook_url: str, message: str = None, embed: dict = None):
    payload = {
        "username": "Hyperliquid Trade Monitor"
    }
    if message:
        payload["content"] = message
    if embed:
        payload["embeds"] = [embed]
        
    headers = {
        "Content-Type": "application/json"
    }
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(
                webhook_url,
                data=json.dumps(payload),
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            return
        except requests.exceptions.RequestException as e:
            if attempt == max_attempts:
                sys.stderr.write(f"Failed to send message to Discord after {max_attempts} attempts: {e}\n")
                return
            time.sleep(2 ** (attempt - 1))

def fetch_spot_meta_index_map():
    global spot_meta_index_cache
    if spot_meta_index_cache is not None:
        return spot_meta_index_cache

    try:
        response = requests.post(
            HYPERLIQUID_INFO_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"type": "spotMeta"}),
            timeout=HYPERLIQUID_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Failed to fetch spotMeta: {e}\n")
        spot_meta_index_cache = {}
        return spot_meta_index_cache

    index_map = {}
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    for item in tokens or []:
        if isinstance(item, dict) and "index" in item and "name" in item:
            index_map[item["index"]] = item["name"]

    spot_meta_index_cache = index_map
    return spot_meta_index_cache

def fetch_collateral_token_index(dex: str):
    try:
        response = requests.post(
            HYPERLIQUID_INFO_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"type": "metaAndAssetCtxs", "dex": dex}),
            timeout=HYPERLIQUID_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Failed to fetch metaAndAssetCtxs for dex {dex}: {e}\n")
        return None

    if not payload or not isinstance(payload, list):
        return None
    meta = payload[0] if payload else None
    if not isinstance(meta, dict):
        return None
    return meta.get("collateralToken")

def get_collateral_ticker_for_dex(dex: str) -> str:
    if not dex:
        return "USDC"
    cached = collateral_ticker_cache.get(dex)
    if cached:
        return cached

    collateral_token = fetch_collateral_token_index(dex)
    if collateral_token is None:
        ticker = "USDC"
    elif isinstance(collateral_token, str):
        if collateral_token.isdigit():
            collateral_token = int(collateral_token)
        else:
            ticker = collateral_token
    elif isinstance(collateral_token, float) and collateral_token.is_integer():
        collateral_token = int(collateral_token)
    if "ticker" not in locals():
        if collateral_token == 0:
            ticker = "USDC"
        else:
            index_map = fetch_spot_meta_index_map()
            ticker = index_map.get(collateral_token, "USDC")

    collateral_ticker_cache[dex] = ticker
    return ticker

def build_trade_embed(trade: Trade, tag: str):
    direction = trade.direction or ""
    # Determine Color and Title based on trade
    color = 0x0099ff # Blue default
    title = f"New {trade.trade_type}"
    
    if "Long" in direction:
        color = 0x00ff00 # Green
        title = f"📈 {direction}"
    elif "Short" in direction:
        color = 0xff0000 # Red
        title = f"📉 {direction}"
    
    if trade.closed_pnl:
        if trade.closed_pnl > 0:
            color = 0x00ff00
            title = f"🟢 Closed Position (Profit)"
        else:
            color = 0xff0000
            title = f"🔴 Closed Position (Loss)"
    
    # Append Tag to Title if it exists
    if tag:
        title += f" (Tag: {tag})"

    # Reconstruct the original text block format
    # Tag is removed from here as it is now in the Title
    address_parts_text = []
    
    # Add "ポジションに変更があったよ！" at the top if trade type is FILL
    if trade.trade_type == "FILL":
        address_parts_text.append("ポジションに変更があったよ！")

    dex = trade.coin.split(":", 1)[0] if trade.coin and ":" in trade.coin else ""
    collateral_ticker = get_collateral_ticker_for_dex(dex)

    address_block_text = "\n".join(address_parts_text)

    display_direction = trade.direction or "Unknown"
    original_format_text = f"""{address_block_text}
Coin: {trade.coin}
Price: {trade.price}
Direction: {display_direction}

Address: https://hypurrscan.io/address/{trade.address}
Ghost: https://app.hyperliquid.xyz/trade/{trade.coin}/{collateral_ticker}?hloa={trade.address}"""

    return {
        "title": title,
        "description": original_format_text, # No code block markdown
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": f"Tx: {trade.tx_hash}"
        }
    }

def process_trade_with_db(webhook_url: str, trade: Trade, db_path: str, tag: str, specific_webhook_url: str = None):
    """DBパスを指定してトレードを処理"""
    global trade_cache, processed_trades, startup_grace_period, last_notification_time

    # 通知するDirectionを限定
    allowed_directions = {"Open Long", "Close Long", "Open Short", "Close Short"}
    if trade.direction not in allowed_directions:
        return

    # ヘルスチェックファイルを更新
    touch_healthcheck_file()

    address_suffix = trade.address[-8:]
    trade_key = make_trade_uid(trade)
    
    # メモリベースの重複チェック
    if trade_key in processed_trades:
        print(f"[{address_suffix}] Trade {trade.tx_hash} already processed in memory, skipping")
        return
    
    # 通知を抑制する（起動時の大量通知を防ぐ）
    current_time = time.time()
    address_startup_time = startup_grace_period.get(trade.address)
    
    if address_startup_time and (current_time - address_startup_time) < 60:
        print(f"[{address_suffix}] Startup grace period - skipping historical trade: {trade.tx_hash}")
        processed_trades.add(trade_key)
        record_trade_in_db(db_path, trade)
        return
    
    ensure_trades_table(db_path)
    if check_trade_exists_in_db(db_path, trade):
        print(f"[{address_suffix}] Trade {trade.tx_hash} already exists in DB, skipping notification")
        processed_trades.add(trade_key)
        return

    # 通知抑制ロジック
    suppression_key = (trade.address, trade.coin, trade.direction)
    last_time = last_notification_time.get(suppression_key)

    if last_time and (current_time - last_time) < NOTIFICATION_SUPPRESSION_SECONDS:
        print(f"[{address_suffix}] Notification for {trade.coin} {trade.direction} suppressed. Last notification was at {datetime.fromtimestamp(last_time).strftime('%Y-%m-%d %H:%M:%S')}")
        processed_trades.add(trade_key)
        record_trade_in_db(db_path, trade)
        return

    # 新しいトレードとして処理
    processed_trades.add(trade_key)
    record_trade_in_db(db_path, trade)
    
    embed = build_trade_embed(trade, tag)

    print(f"[{address_suffix}] Sending Discord notification for new trade: {trade.tx_hash}")
    send_to_discord(webhook_url, embed=embed)

    if specific_webhook_url:
        print(f"[{address_suffix}] Sending Discord notification to specific webhook: {trade.tx_hash}")
        send_to_discord(specific_webhook_url, embed=embed)

    last_notification_time[suppression_key] = current_time

def ensure_trades_table(db_path: str) -> None:
    try:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_uid TEXT,
                    tx_hash TEXT,
                    address TEXT NOT NULL,
                    coin TEXT,
                    direction TEXT,
                    timestamp TEXT,
                    seen_at INTEGER NOT NULL
                )
            """)

            columns_info = list(conn.execute("PRAGMA table_info(trades)"))
            columns = {row[1] for row in columns_info}
            tx_hash_is_primary = any(row[1] == "tx_hash" and row[5] == 1 for row in columns_info)

            if tx_hash_is_primary:
                conn.execute("ALTER TABLE trades RENAME TO trades_legacy")
                conn.execute("""
                    CREATE TABLE trades (
                        trade_uid TEXT,
                        tx_hash TEXT,
                        address TEXT NOT NULL,
                        coin TEXT,
                        direction TEXT,
                        timestamp TEXT,
                        seen_at INTEGER NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO trades (trade_uid, tx_hash, address, coin, direction, timestamp, seen_at)
                    SELECT
                        address || '|' || COALESCE(tx_hash, '') || '|' || COALESCE(coin, '') || '|' ||
                        COALESCE(direction, '') || '|' || COALESCE(timestamp, ''),
                        tx_hash,
                        address,
                        coin,
                        direction,
                        timestamp,
                        seen_at
                    FROM trades_legacy
                """)
                conn.execute("DROP TABLE trades_legacy")
                columns = {"trade_uid", "tx_hash", "address", "coin", "direction", "timestamp", "seen_at"}

            if "trade_uid" not in columns:
                conn.execute("ALTER TABLE trades ADD COLUMN trade_uid TEXT")

            conn.execute("""
                UPDATE trades
                SET trade_uid = COALESCE(
                    trade_uid,
                    address || '|' || COALESCE(tx_hash, '') || '|' || COALESCE(coin, '') || '|' ||
                    COALESCE(direction, '') || '|' || COALESCE(timestamp, '')
                )
                WHERE trade_uid IS NULL
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_trade_uid
                ON trades(trade_uid)
            """)
    except sqlite3.Error as e:
        print(f"SQLite error ensuring DB schema ({os.path.abspath(db_path)}): {e}")

def record_trade_in_db(db_path: str, trade: Trade) -> None:
    if not trade.tx_hash:
        return False
    trade_uid = make_trade_uid(trade)
    try:
        ensure_trades_table(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO trades (trade_uid, tx_hash, address, coin, direction, timestamp, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_uid,
                    trade.tx_hash,
                    trade.address,
                    trade.coin,
                    trade.direction,
                    trade.timestamp.isoformat() if trade.timestamp else None,
                    int(time.time()),
                ),
            )
        return True
    except sqlite3.Error as e:
        print(f"SQLite error storing trade in DB ({os.path.abspath(db_path)}): {e}")
        return False

def bootstrap_seen_fills(address: str, db_path: str) -> None:
    """
    Seed recent fills into DB once when DB is empty.
    This prevents replay notifications right after down/up.
    """
    ensure_trades_table(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades")
            if cur.fetchone()[0] > 0:
                return
    except sqlite3.Error as e:
        print(f"[{address[-8:]}] Failed to inspect DB for bootstrap: {e}")
        return

    info = Info(skip_ws=True)
    try:
        fills = info.user_fills(address) or []
    except Exception as e:
        print(f"[{address[-8:]}] Failed to bootstrap user_fills: {e}")
        return
    finally:
        try:
            if hasattr(info, "ws_manager") and info.ws_manager:
                info.ws_manager.stop()
        except Exception:
            pass

    seeded = 0
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        tx_hash = fill.get("hash")
        if not tx_hash:
            continue
        trade = HyperliquidUserFillsMonitor.build_trade_from_fill(fill, address)
        if record_trade_in_db(db_path, trade):
            seeded += 1
    print(f"[{address[-8:]}] Bootstrap seeded {seeded} fills into DB (no notifications).")

def check_trade_exists_in_db(db_path: str, trade: Trade) -> bool:
    """DBに指定されたfill相当のトレードが既に存在するかチェック"""
    try:
        if not os.path.exists(db_path):
            return False
        ensure_trades_table(db_path)
        trade_uid = make_trade_uid(trade)
        timestamp = trade.timestamp.isoformat() if trade.timestamp else None
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                FROM trades
                WHERE trade_uid = ?
                   OR (
                        tx_hash = ?
                    AND address = ?
                    AND coin = ?
                    AND direction = ?
                    AND timestamp = ?
                   )
                LIMIT 1
                """,
                (
                    trade_uid,
                    trade.tx_hash,
                    trade.address,
                    trade.coin,
                    trade.direction,
                    timestamp,
                ),
            )
            row = cursor.fetchone()
            return row is not None
    except sqlite3.Error as e:
        print(f"SQLite error checking trade in DB: {e}")
        return False
    except Exception as e:
        print(f"Error checking trade in DB: {e}")
        return False

def run_test_preview(
    addresses_file: str,
    enable_posting: bool = False,
    timeout_seconds: int = None,
    max_entries: int = None
):
    if not os.path.exists(addresses_file):
        print(f"Addresses file not found: {addresses_file}")
        return

    addresses = load_addresses(addresses_file)
    timeout_seconds = int(timeout_seconds if timeout_seconds is not None else os.getenv("TEST_PREVIEW_TIMEOUT_SECONDS", 60))
    max_entries = int(max_entries if max_entries is not None else os.getenv("TEST_PREVIEW_MAX_ENTRIES", 10))
    count = 0
    seen_hashes = set()
    pending = set(addresses.keys())
    printed_per_address = defaultdict(int)
    count_lock = threading.Lock()
    done_event = threading.Event()

    print(f"Starting websocket preview for {len(addresses)} addresses (timeout: {timeout_seconds}s)")
    if enable_posting:
        print("WARNING: Discord posting is ENABLED for this test.")
    print(f"Printing up to {max_entries} fills")

    def callback(msg):
        nonlocal count
        if not isinstance(msg, dict):
            return
        data = msg.get("data") or {}
        fills = data.get("fills") or []
        address = (data.get("user") or "").lower()
        if address not in addresses:
            return
        for fill in fills:
            tx_hash = fill.get("hash")
            if not tx_hash:
                continue
            with count_lock:
                if tx_hash in seen_hashes:
                    continue
                if count >= max_entries:
                    done_event.set()
                    return
                if pending and address not in pending:
                    continue
                seen_hashes.add(tx_hash)
                count += 1
                printed_per_address[address] += 1
                if printed_per_address[address] >= 1 and address in pending:
                    pending.remove(address)
            trade = HyperliquidUserFillsMonitor.build_trade_from_fill(fill, address)
            info = addresses.get(trade.address)
            tag = info.get('tag') if info else None
            specific_webhook = info.get('webhook') if info else None
            embed = build_trade_embed(trade, tag)
            print("\n-----")
            print(f"Title: {embed['title']}")
            print(embed["description"])
            print(embed["footer"]["text"])

            if enable_posting:
                webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
                if webhook_url:
                    print("Sending to global webhook...")
                    send_to_discord(webhook_url, embed=embed)
                if specific_webhook:
                    print("Sending to specific webhook...")
                    send_to_discord(specific_webhook, embed=embed)

            if count >= max_entries or not pending:
                done_event.set()
                return

    info = Info()
    for address in addresses.keys():
        info.subscribe({"type": "userFills", "user": address}, callback)

    done_event.wait(timeout_seconds)
    if hasattr(info, "ws_manager") and info.ws_manager:
        try:
            info.ws_manager.ws.close()
        except Exception as e:
            print(f"Error closing websocket: {e}")

    with count_lock:
        printed = count
    if printed < max_entries:
        print(f"\nTimeout reached. Printed {printed}/{max_entries} fills.")

def write_pidfile(pidfile):
    try:
        with open(pidfile, 'w') as f:
            f.write(str(os.getpid()))
        print(f"PID file created: {pidfile}")
    except IOError as e:
        sys.stderr.write(f"Failed to write pidfile: {e}\n")

def remove_pidfile(pidfile):
    try:
        if os.path.exists(pidfile):
            os.remove(pidfile)
            print(f"PID file removed: {pidfile}")
    except OSError as e:
        print(f"Error removing pidfile: {e}")

async def monitor_addresses_async(webhook_url: str, addresses: dict):
    """複数アドレスを単一 WebSocket 接続で監視し、切断時に自動再接続する"""
    global startup_grace_period, monitor_instances

    address_state = {}
    for address, info in addresses.items():
        db_path = os.path.join(DB_DIRECTORY, f"trades_{address[-8:]}.db")
        ensure_trades_table(db_path)
        bootstrap_seen_fills(address, db_path)
        address_state[address] = {
            'tag': info.get('tag'),
            'webhook': info.get('webhook'),
            'db_path': db_path,
        }

    shared_state = {
        'last_trade_time': time.time(),
        'connection_dead': threading.Event()
    }

    def monitor_callback(trade):
        shared_state['last_trade_time'] = time.time()
        address_info = address_state.get(trade.address)
        if not address_info:
            return
        return process_trade_with_db(
            webhook_url,
            trade,
            address_info['db_path'],
            address_info['tag'],
            address_info['webhook'],
        )

    while True:
        monitor = None
        monitor_thread = None
        shared_state['connection_dead'].clear()
        shared_state['last_trade_time'] = time.time()

        try:
            print(f"Initializing shared monitor for {len(addresses)} addresses")
            for index, (address, info) in enumerate(addresses.items()):
                tag = info.get('tag')
                print(f"[{index}] Preparing subscription for {address}" + (f" ({tag})" if tag else ""))
                startup_grace_period[address] = time.time()

            monitor = HyperliquidUserFillsMonitor(
                addresses=list(addresses.keys()),
                callback=monitor_callback
            )
            monitor_instances['shared'] = monitor

            try:
                def patched_send_ping(ws_manager_instance):
                    """Patched send_ping that signals when the websocket is closed."""
                    print("Starting patched ping thread for shared websocket.")
                    while not ws_manager_instance.ws.closed and not shared_state['connection_dead'].is_set():
                        try:
                            ws_manager_instance.ws.send(json.dumps({"method": "ping"}))
                            time.sleep(5)
                        except WebSocketConnectionClosedException:
                            print("Ping thread: WebSocket connection closed. Signaling for reconnect.")
                            shared_state['connection_dead'].set()
                            if monitor:
                                monitor.stop()
                            break
                        except Exception as e:
                            print(f"Error in patched ping thread for shared websocket: {e}. Signaling for reconnect.")
                            shared_state['connection_dead'].set()
                            if monitor:
                                monitor.stop()
                            break
                    print("Patched ping thread for shared websocket terminated.")

                if hasattr(monitor, 'info') and hasattr(monitor.info, 'ws_manager') and hasattr(monitor.info.ws_manager, 'send_ping'):
                    ws_manager = monitor.info.ws_manager
                    ws_manager.send_ping = patched_send_ping.__get__(ws_manager)
                    print("Successfully patched shared 'send_ping' method.")
                else:
                    sys.stderr.write("WARNING: Could not find 'monitor.info.ws_manager.send_ping' method to patch.\n")
            except Exception as e:
                sys.stderr.write(f"WARNING: An error occurred while applying the ping thread patch: {e}\n")

            error_container = {'error': None}

            def start_monitor_thread():
                """A thread to run the blocking monitor.start() call."""
                try:
                    print("Starting shared monitor.start() in a new thread.")
                    monitor.start()
                except Exception as e:
                    error_container['error'] = e
                    sys.stderr.write(f"Error inside shared monitor thread: {e}\n")
                finally:
                    print("Shared monitor thread has finished.")

            monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
            monitor_thread.start()

            await asyncio.sleep(2)
            if error_container['error']:
                raise error_container['error']

            print(f"Shared monitor started successfully for {len(addresses)} addresses. Grace period active for 60s.")

            while monitor_thread.is_alive():
                if shared_state['connection_dead'].is_set():
                    print("Main loop detected dead shared connection signal. Breaking to reconnect.")
                    break

                if (time.time() - shared_state['last_trade_time']) > WEBSOCKET_ACTIVITY_TIMEOUT:
                    print(f"No trade activity for over {WEBSOCKET_ACTIVITY_TIMEOUT} seconds on shared websocket. Forcing reconnect.")
                    shared_state['connection_dead'].set()
                    if monitor:
                        monitor.stop()
                    break

                await asyncio.sleep(10)

            if error_container['error']:
                print(f"Shared monitor thread stopped due to an error: {error_container['error']}. Reconnecting...")
            else:
                print("Shared monitor thread stopped. Reconnecting...")

        except Exception as e:
            sys.stderr.write(f"An exception occurred in the shared monitor loop: {e}\n")

        finally:
            if 'shared' in monitor_instances:
                try:
                    print("Cleaning up shared monitor instance.")
                    monitor_instances['shared'].stop()
                except Exception as e:
                    sys.stderr.write(f"Error stopping shared monitor during cleanup: {e}\n")
                del monitor_instances['shared']

            wait_time = 30
            print(f"Waiting {wait_time} seconds before reconnecting shared websocket...")
            await asyncio.sleep(wait_time)

async def run_multi_monitor_async(webhook_url: str, addresses: dict):
    """複数アドレスの非同期監視"""
    print(f"Starting multi-address monitor for {len(addresses)} addresses")

    for i, (address, info) in enumerate(addresses.items()):
        tag = info.get('tag')
        print(f"Configured address {i}: {address}" + (f" ({tag})" if tag else ""))

    try:
        await monitor_addresses_async(webhook_url, addresses)
    except Exception as e:
        print(f"Error in multi-monitor: {e}")
        raise

def signal_handler(signum, frame):
    global monitor_instances, main_loop
    print(f"Received signal {signum}, shutting down...")

    # すべての監視インスタンスを停止
    for address, monitor in monitor_instances.items():
        try:
            print(f"Stopping monitor for {address}")
            monitor.stop()
        except Exception as e:
            print(f"Error stopping monitor for {address}: {e}")
    
    monitor_instances.clear()
    
    if main_loop and main_loop.is_running():
        main_loop.stop()
    
    sys.exit(0)

def start_daemon(script_path, addresses_file):
    """単一プロセスでのデーモン起動"""
    log_file = '/tmp/hyperliquid_monitor_multi.log'
    error_file = '/tmp/hyperliquid_monitor_multi_error.log'
    pidfile = '/tmp/hyperliquid_monitor_multi.pid'
    
    remove_pidfile(pidfile)
    
    cmd = [sys.executable, script_path, 'monitor', addresses_file, '--background']
    
    print(f"Starting multi-address daemon with command: {' '.join(cmd)}")
    print(f"Logs will be written to: {log_file}")
    print(f"Errors will be written to: {error_file}")
    
    try:
        with open(log_file, 'a') as log_f, open(error_file, 'a') as err_f:
            process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                start_new_session=True, 
                cwd=os.getcwd()
            )
            
            with open(pidfile, 'w') as f:
                f.write(str(process.pid))
            
            print(f"Multi-address daemon started with PID: {process.pid}")
            print(f"PID file: {pidfile}")
            
            time.sleep(2)
            if process.poll() is None:
                print("Multi-address daemon started successfully!")
                print("\nManagement commands:")
                print(f"  Check status: ps aux | grep {os.path.basename(script_path)}")
                print(f"  Stop daemon: kill $(cat {pidfile})")
                print(f"  View logs: tail -f {log_file}")
                print(f"  View errors: tail -f {error_file}")
                return True
            else:
                print("Multi-address daemon failed to start!")
                return False
                
    except Exception as e:
        print(f"Failed to start multi-address daemon: {e}")
        return False

def run_monitor(webhook_url: str, addresses_file: str, background_mode: bool = False):
    """メイン監視ループ（複数アドレス対応）"""
    global main_loop
    
    addresses = load_addresses(addresses_file)
    
    print(f"Loading {len(addresses)} addresses:")
    for i, (addr, info) in enumerate(addresses.items()):
        tag = info.get('tag')
        print(f"  {i+1}: {addr}" + (f" (Tag: {tag})" if tag else ""))
    
    # シグナルハンドラーの設定
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    print(f"Process PID: {os.getpid()}")
    
    try:
        # 新しいイベントループを作成して実行
        if sys.version_info >= (3, 7):
            asyncio.run(run_multi_monitor_async(webhook_url, addresses))
        else:
            # Python 3.6以下の場合
            loop = asyncio.get_event_loop()
            main_loop = loop
            loop.run_until_complete(run_multi_monitor_async(webhook_url, addresses))
            
    except KeyboardInterrupt:
        print("Keyboard interrupt received, stopping...")
    except Exception as e:
        error_msg = f"Multi-monitor error: {e}"
        print(error_msg)
        sys.exit(1)
    finally:
        # クリーンアップ
        for address, monitor in monitor_instances.items():
            try:
                monitor.stop()
            except:
                pass
        monitor_instances.clear()

def build_parser():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Trade Monitor (Multi-Address WebSocket Support)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", metavar="command")

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Run trade monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    monitor_parser.add_argument(
        "addresses_file",
        nargs="?",
        default="addresses.txt",
        help="Path to addresses file"
    )
    monitor_parser.add_argument(
        "-d", "--daemon",
        action="store_true",
        help="Run as background daemon (single process monitoring all addresses)"
    )
    monitor_parser.add_argument(
        "--background",
        action="store_true",
        help="Internal flag used by daemon child process"
    )
    monitor_parser.add_argument(
        "--webhook-url",
        help="Override DISCORD_WEBHOOK_URL environment variable"
    )

    tests_parser = subparsers.add_parser(
        "tests",
        help="Preview recent fills without running full monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    tests_parser.add_argument(
        "-f", "--addresses-file",
        default="addresses.txt",
        help="Path to addresses file used in preview"
    )
    tests_parser.add_argument(
        "--post",
        action="store_true",
        help="Also post preview messages to Discord"
    )
    tests_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("TEST_PREVIEW_TIMEOUT_SECONDS", 60)),
        help="Seconds to wait before stopping preview"
    )
    tests_parser.add_argument(
        "--max-entries",
        type=int,
        default=int(os.getenv("TEST_PREVIEW_MAX_ENTRIES", 10)),
        help="Maximum fills to print before stopping preview"
    )
    tests_parser.add_argument(
        "legacy_action",
        nargs="?",
        choices=["post"],
        help="Legacy positional mode (equivalent to --post)"
    )
    return parser

def parse_args():
    parser = build_parser()
    argv = sys.argv[1:]

    # Backward compatibility: `python script.py addresses.txt`
    if argv and argv[0] not in {"monitor", "tests"} and not argv[0].startswith("-"):
        argv = ["monitor", *argv]

    if not argv:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args(argv)

def main():
    args = parse_args()

    if args.command == "tests":
        enable_posting = args.post or args.legacy_action == "post"
        run_test_preview(
            args.addresses_file,
            enable_posting=enable_posting,
            timeout_seconds=args.timeout_seconds,
            max_entries=args.max_entries
        )
        sys.exit(0)

    addresses_file = args.addresses_file
    webhook_url = args.webhook_url or os.getenv('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        sys.stderr.write("Error: DISCORD_WEBHOOK_URL not found in environment variables.\n")
        sys.stderr.write("Please create a .env file with DISCORD_WEBHOOK_URL=your_webhook_url\n")
        sys.exit(1)

    if not os.path.exists(addresses_file):
        sys.stderr.write(f"Addresses file not found: {addresses_file}\n")
        sys.exit(1)

    if args.daemon and not args.background:
        script_path = os.path.abspath(sys.argv[0])
        addresses = load_addresses(addresses_file)
        
        print(f"Starting daemon for {len(addresses)} addresses in single process")
        start_daemon(script_path, addresses_file)
        sys.exit(0)

    run_monitor(webhook_url, addresses_file, args.background)

if __name__ == "__main__":
    main()
