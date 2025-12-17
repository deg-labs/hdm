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
from hyperliquid_monitor.monitor import HyperliquidMonitor
from hyperliquid_monitor.types import Trade
from datetime import datetime
from collections import defaultdict
import requests
import json
from dotenv import load_dotenv

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

processed_trades = set()
startup_grace_period = {}

original_signal = signal.signal

def patched_signal(sig, handler):
    """スレッド内でのシグナル設定を無効化"""
    if threading.current_thread() != threading.main_thread():
        # メインスレッド以外では何もしない
        return None
    return original_signal(sig, handler)

signal.signal = patched_signal

def touch_healthcheck_file():
    """ヘルスチェックファイルをtouch"""
    try:
        with open(HEALTHCHECK_FILE, 'a'):
            os.utime(HEALTHCHECK_FILE, None)
    except Exception as e:
        sys.stderr.write(f"Failed to touch healthcheck file: {e}\n")

def send_to_discord(webhook_url: str, message: str):
    payload = {
        "content": message,
        "username": "Hyperliquid Trade Monitor"
    }
    headers = {
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers=headers
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        sys.stderr.write(f"Failed to send message to Discord: {e}\n")

def process_trade_with_db(webhook_url: str, trade: Trade, db_path: str, tag: str):
    """DBパスを指定してトレードを処理"""
    global trade_cache, processed_trades, startup_grace_period, last_notification_time

    # ヘルスチェックファイルを更新
    touch_healthcheck_file()

    address_suffix = trade.address[-8:]
    trade_key = f"{trade.address}:{trade.tx_hash}"
    
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
        return
    
    if os.path.exists(db_path) and check_trade_exists_in_db(db_path, trade.tx_hash):
        print(f"[{address_suffix}] Trade {trade.tx_hash} already exists in DB, skipping notification")
        processed_trades.add(trade_key)
        return

    # 通知抑制ロジック
    suppression_key = (trade.address, trade.coin, trade.direction)
    last_time = last_notification_time.get(suppression_key)

    if last_time and (current_time - last_time) < NOTIFICATION_SUPPRESSION_SECONDS:
        print(f"[{address_suffix}] Notification for {trade.coin} {trade.direction} suppressed. Last notification was at {datetime.fromtimestamp(last_time).strftime('%Y-%m-%d %H:%M:%S')}")
        processed_trades.add(trade_key)
        return

    # 新しいトレードとして処理
    processed_trades.add(trade_key)
    
    trade_cache[trade.tx_hash].append(trade)
    trades = trade_cache[trade.tx_hash]
    total_size = sum(t.size for t in trades)
    timestamp = trade.timestamp.strftime('%Y-%m-%d %H:%M:%S')

    discord_msg = ""

    if len(trades) == 1:
        address_parts = [f"Address: https://hypurrscan.io/address/{trade.address}"]
        if tag:
            address_parts.append(f"Tag: {tag}")
        address_block = "\n".join(address_parts)

        discord_msg = f"""**[{timestamp}] New {trade.trade_type}**"""
        if trade.trade_type == "FILL":
            discord_msg += " ポジションに変更があったよ！"
        discord_msg += f"""
{address_block}
```
Coin: {trade.coin}
Price: {trade.price}"""
        
        if trade.trade_type == "FILL":
            discord_msg += f"\nDirection: {trade.direction}"
        
        if trade.closed_pnl:
            pnl_emoji = "🟢" if trade.closed_pnl > 0 else "🔴"
            discord_msg += f"\nPnL: {pnl_emoji} {trade.closed_pnl:.2f}"
        
        discord_msg += "\n```"

        if discord_msg:
            print(f"[{address_suffix}] Sending Discord notification for new trade: {trade.tx_hash}")
            send_to_discord(webhook_url, discord_msg)
            # 通知を送信したら、時刻を更新
            last_notification_time[suppression_key] = current_time

def check_trade_exists_in_db(db_path: str, tx_hash: str) -> bool:
    """DBに指定されたtx_hashのトレードが既に存在するかチェック"""
    import sqlite3
    try:
        # DBファイルが存在しない場合は存在しないと判定
        if not os.path.exists(db_path):
            return False
            
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # テーブルの存在確認
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='trades'
        """)
        
        if not cursor.fetchone():
            conn.close()
            return False  # ログ出力を削除（起動時の大量出力を防ぐ）
        
        # tx_hash列の存在確認
        cursor.execute("PRAGMA table_info(trades)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'tx_hash' not in columns:
            conn.close()
            return False
        
        # tradesテーブルでtx_hashをチェック
        cursor.execute("SELECT COUNT(*) FROM trades WHERE tx_hash = ?", (tx_hash,))
        count = cursor.fetchone()[0]
        
        conn.close()
        return count > 0
        
    except sqlite3.Error as e:
        print(f"SQLite error checking trade in DB: {e}")
        return False
    except Exception as e:
        print(f"Error checking trade in DB: {e}")
        return False

def load_addresses(file_path: str) -> dict:
    addresses = {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                parts = line.split(',', 1)
                address = parts[0].strip().lower()
                
                if address:
                    tag = parts[1].strip() if len(parts) > 1 else None
                    addresses[address] = tag
    except IOError as e:
        sys.stderr.write(f"Error reading addresses file: {e}\n")
        sys.exit(1)

    if not addresses:
        sys.stderr.write("No addresses found in addresses file\n")
        sys.exit(1)

    return addresses

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

async def monitor_address_async(webhook_url: str, address: str, tag: str, address_index: int):
    """非同期で単一アドレスを監視し、切断時に自動再接続する"""
    global startup_grace_period, monitor_instances
    
    db_path = os.path.join(DB_DIRECTORY, f"trades_{address[-8:]}.db")

    # Create a shared state object for communication between threads
    shared_state = {
        'last_trade_time': time.time(),
        'connection_dead': threading.Event()
    }

    def create_callback(addr, db_file, tag_param):
        def callback(trade):
            # Update the timestamp on each new trade
            shared_state['last_trade_time'] = time.time()
            return process_trade_with_db(webhook_url, trade, db_file, tag_param)
        return callback

    # Create the callback once
    monitor_callback = create_callback(address, db_path, tag)

    while True:  # The main reconnection loop
        monitor = None
        monitor_thread = None
        
        # Reset state for the new connection attempt
        shared_state['connection_dead'].clear()
        shared_state['last_trade_time'] = time.time()

        try:
            print(f"[{address_index}] Initializing monitor for address: {address}" + (f" ({tag})" if tag else ""))
            
            startup_grace_period[address] = time.time()
            
            monitor = HyperliquidMonitor(
                addresses=[address],
                db_path=db_path,
                callback=monitor_callback
            )
            monitor_instances[address] = monitor

            # --- Monkey-patching the send_ping method ---
            try:
                def patched_send_ping(ws_manager_instance):
                    """Patched send_ping that signals when the websocket is closed."""
                    print(f"[{address_index}] Starting patched ping thread for {address}.")
                    while not ws_manager_instance.ws.closed and not shared_state['connection_dead'].is_set():
                        try:
                            ws_manager_instance.ws.send(json.dumps({"method": "ping"}))
                            time.sleep(5)
                        except WebSocketConnectionClosedException:
                            print(f"[{address_index}] Ping thread: WebSocket connection closed. Signaling for reconnect.")
                            shared_state['connection_dead'].set()
                            if monitor:
                                monitor.stop()
                            break
                        except Exception as e:
                            print(f"[{address_index}] Error in patched ping thread for {address}: {e}. Signaling for reconnect.")
                            shared_state['connection_dead'].set()
                            if monitor:
                                monitor.stop()
                            break
                    print(f"[{address_index}] Patched ping thread for {address} terminated.")

                if hasattr(monitor, 'info') and hasattr(monitor.info, 'ws_manager') and hasattr(monitor.info.ws_manager, 'send_ping'):
                    ws_manager = monitor.info.ws_manager
                    ws_manager.send_ping = patched_send_ping.__get__(ws_manager)
                    print(f"[{address_index}] Successfully patched 'send_ping' method.")
                else:
                    sys.stderr.write(f"[{address_index}] WARNING: Could not find 'monitor.info.ws_manager.send_ping' method to patch.\n")
            except Exception as e:
                sys.stderr.write(f"[{address_index}] WARNING: An error occurred while applying the ping thread patch: {e}\n")
            
            error_container = {'error': None}
            
            def start_monitor_thread():
                """A thread to run the blocking monitor.start() call."""
                try:
                    print(f"[{address_index}] Starting monitor.start() for {address} in a new thread.")
                    monitor.start()
                except Exception as e:
                    error_container['error'] = e
                    sys.stderr.write(f"[{address_index}] Error inside monitor thread for {address}: {e}\n")
                finally:
                    print(f"[{address_index}] Monitor thread for {address} has finished.")

            monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
            monitor_thread.start()
            
            await asyncio.sleep(2)
            if error_container['error']:
                raise error_container['error']

            print(f"[{address_index}] Monitor for {address} started successfully. Grace period active for 60s.")
            
            # Main loop to check thread health and connection status
            while monitor_thread.is_alive():
                # Check 1: Signal from the ping thread
                if shared_state['connection_dead'].is_set():
                    print(f"[{address_index}] Main loop detected dead connection signal. Breaking to reconnect.")
                    break
                
                # Check 2: Inactivity timeout
                if (time.time() - shared_state['last_trade_time']) > WEBSOCKET_ACTIVITY_TIMEOUT:
                    print(f"[{address_index}] No trade activity for over {WEBSOCKET_ACTIVITY_TIMEOUT} seconds. Forcing reconnect.")
                    shared_state['connection_dead'].set() # Signal other threads
                    if monitor:
                        monitor.stop()
                    break

                await asyncio.sleep(10)
            
            if error_container['error']:
                print(f"[{address_index}] Monitor thread for {address} stopped due to an error: {error_container['error']}. Reconnecting...")
            else:
                print(f"[{address_index}] Monitor thread for {address} stopped. Reconnecting...")

        except Exception as e:
            sys.stderr.write(f"[{address_index}] An exception occurred in the monitor loop for {address}: {e}\n")
        
        finally:
            if address in monitor_instances:
                try:
                    print(f"[{address_index}] Cleaning up monitor instance for {address}.")
                    monitor_instances[address].stop()
                except Exception as e:
                    sys.stderr.write(f"[{address_index}] Error stopping monitor during cleanup: {e}\n")
                del monitor_instances[address]
            
            wait_time = 30
            print(f"[{address_index}] Waiting {wait_time} seconds before reconnecting {address}...")
            await asyncio.sleep(wait_time)

async def run_multi_monitor_async(webhook_url: str, addresses: dict):
    """複数アドレスの非同期監視"""
    print(f"Starting multi-address monitor for {len(addresses)} addresses")
    
    # 各アドレスの監視タスクを作成
    tasks = []
    for i, (address, tag) in enumerate(addresses.items()):
        task = asyncio.create_task(
            monitor_address_async(webhook_url, address, tag, i)
        )
        tasks.append(task)
        monitor_tasks[address] = task
        print(f"Created monitoring task for address {i}: {address}" + (f" ({tag})" if tag else ""))
    
    try:
        # すべてのタスクを並行実行
        await asyncio.gather(*tasks)
    except Exception as e:
        print(f"Error in multi-monitor: {e}")
        raise

def signal_handler(signum, frame):
    global monitor_instances, main_loop
    print(f"Received signal {signum}, shutting down...")
    
    # パッチを元に戻す
    signal.signal = original_signal
    
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
    
    cmd = [sys.executable, script_path, addresses_file, '--background']
    
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
    for i, (addr, tag) in enumerate(addresses.items()):
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

def main():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Trade Monitor (Multi-Address WebSocket Support)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "addresses_file",
        nargs="?",
        help="Path to the file containing addresses to monitor"
    )
    parser.add_argument(
        "-d", "--daemon",
        action="store_true",
        help="Run as background daemon (single process monitoring all addresses)"
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help=argparse.SUPPRESS
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        sys.stderr.write("Error: DISCORD_WEBHOOK_URL not found in environment variables.\n")
        sys.stderr.write("Please create a .env file with DISCORD_WEBHOOK_URL=your_webhook_url\n")
        sys.exit(1)

    if not os.path.exists(args.addresses_file):
        sys.stderr.write(f"Addresses file not found: {args.addresses_file}\n")
        sys.exit(1)

    if args.daemon and not args.background:
        script_path = os.path.abspath(sys.argv[0])
        addresses = load_addresses(args.addresses_file)
        
        print(f"Starting daemon for {len(addresses)} addresses in single process")
        start_daemon(script_path, args.addresses_file)
        sys.exit(0)

    run_monitor(webhook_url, args.addresses_file, args.background)

if __name__ == "__main__":
    main()
