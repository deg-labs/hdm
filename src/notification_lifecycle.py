"""Durable Discord notification sessions: POST -> PATCH updates -> final POST.

Each wallet/coin/destination owns a session. Fills are never discarded merely
because another notification was sent recently. SQLite persists the latest fill
and message ID, so the idle sweep survives process restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import discord_retention as retention

logger = logging.getLogger("hdm.notifications")


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_sessions (
            coin TEXT NOT NULL,
            webhook_url TEXT NOT NULL,
            first_embed TEXT NOT NULL,
            latest_fill TEXT NOT NULL,
            tag TEXT,
            event_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            last_event_at REAL NOT NULL,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (coin, webhook_url)
        )
    """)


def _fill_snapshot(trade):
    return {
        "address": trade.address, "coin": trade.coin,
        "direction": trade.direction, "price": trade.price,
        "size": trade.size, "tx_hash": trade.tx_hash,
        "timestamp": trade.timestamp.isoformat() if trade.timestamp else "",
    }


def enqueue(db_path, trade, tag, webhook_urls, first_embed, now=None):
    """Persist a new fill for each target. Caller already deduplicated the fill."""
    now = time.time() if now is None else now
    snapshot = json.dumps(_fill_snapshot(trade), ensure_ascii=False)
    with sqlite3.connect(db_path, timeout=10) as conn:
        ensure_schema(conn)
        for webhook_url in dict.fromkeys(url for url in webhook_urls if url):
            conn.execute("""
                INSERT INTO notification_sessions
                    (coin, webhook_url, first_embed, latest_fill, tag,
                     event_count, last_event_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(coin, webhook_url) DO UPDATE SET
                    latest_fill=excluded.latest_fill,
                    tag=excluded.tag,
                    event_count=notification_sessions.event_count + 1,
                    last_event_at=excluded.last_event_at
            """, (
                trade.coin, webhook_url,
                json.dumps(first_embed, ensure_ascii=False), snapshot,
                tag, now,
            ))


def active_message_ids(conn):
    """Do not delete active leading POSTs during Discord retention sweeps."""
    ensure_schema(conn)
    return {
        row[0] for row in conn.execute(
            "SELECT message_id FROM notification_sessions WHERE message_id IS NOT NULL"
        )
    }


def _session_embed(row, final=False):
    fill = json.loads(row["latest_fill"])
    direction = fill.get("direction") or "Unknown"
    coin = fill.get("coin") or row["coin"]
    marker = "✅" if final else "📈" if "Long" in direction else "📉"
    suffix = "ポジション変更完了" if final else "更新中"
    title = f"{marker} {coin} {suffix}"
    if row["tag"]:
        title += f" (Tag: {row['tag']})"
    description = (
        f"約定数: {row['event_count']}回\n"
        f"最新方向: {direction}\n"
        f"最終約定価格: {fill.get('price')}\n"
        f"最終約定サイズ: {fill.get('size')}\n"
        f"Address: https://hypurrscan.io/address/{fill.get('address')}"
    )
    return {
        "title": title,
        "description": description,
        "color": 0x2ECC71 if final else 0x3498DB,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Latest Tx: {fill.get('tx_hash') or 'N/A'}"},
    }


def _post(webhook, embed):
    """Return (outcome, message_id, retry_delay); ambiguous POSTs are logged."""
    try:
        response = requests.post(
            retention.post_url(webhook),
            json={"username": "Hyperliquid Trade Monitor", "embeds": [embed],
                  "allowed_mentions": {"parse": []}},
            timeout=10,
        )
        if response.status_code == 429:
            return "retry", None, retention.retry_delay(response.json())
        if response.status_code >= 500:
            return "retry", None, 10.0
        response.raise_for_status()
        payload = response.json()
        message_id = payload.get("id") if isinstance(payload, dict) else None
        if not message_id:
            logger.error("Discord POST returned no message ID")
            return "unknown", None, 0
        return "ok", str(message_id), 0
    except requests.exceptions.ConnectTimeout:
        return "retry", None, 10.0
    except (requests.exceptions.RequestException, ValueError, TypeError) as error:
        logger.error("Discord POST outcome uncertain error_type=%s", type(error).__name__)
        return "unknown", None, 0


def _patch(webhook, message_id, embed):
    try:
        response = requests.patch(
            retention.message_url(webhook, message_id),
            json={"embeds": [embed], "allowed_mentions": {"parse": []}},
            timeout=10,
        )
        if response.status_code == 429:
            return "retry", retention.retry_delay(response.json())
        if response.status_code == 404:
            try:
                body = response.json()
            except ValueError:
                body = None
            if retention.error_code(body) == 10008:
                return "missing", 0
        if response.status_code >= 500:
            return "retry", 10.0
        response.raise_for_status()
        return "ok", 0
    except requests.exceptions.ConnectTimeout:
        return "retry", 10.0
    except (requests.exceptions.RequestException, ValueError, TypeError) as error:
        logger.warning("Discord PATCH failed error_type=%s", type(error).__name__)
        return "retry", 10.0


def _persist_message(db_path, message_id, webhook_url):
    # Uses the existing crash-recovery journal for Discord retention.
    retention.record_message(db_path, message_id, webhook_url, logger)


def _load_due(conn, now):
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT * FROM notification_sessions
        WHERE next_attempt_at <= ?
        ORDER BY last_event_at, coin
    """, (now,)).fetchall()


def _update(db_path, row, changes):
    columns = ", ".join(f"{column}=?" for column in changes)
    with sqlite3.connect(db_path, timeout=10) as conn:
        # New events may increase event_count concurrently, so never overwrite
        # latest_fill or event_count after a network operation.
        conn.execute(
            f"UPDATE notification_sessions SET {columns} "
            "WHERE coin=? AND webhook_url=? AND message_id IS ?",
            (*changes.values(), row["coin"], row["webhook_url"], row["message_id"]),
        )


def dispatch_db(db_path, quiet_seconds, now=None):
    """Perform one delivery sweep outside the WebSocket callback."""
    now = time.time() if now is None else now
    with sqlite3.connect(db_path, timeout=10) as conn:
        ensure_schema(conn)
        rows = _load_due(conn, now)
    for row in rows:
        webhook = row["webhook_url"]
        if not row["message_id"]:
            outcome, message_id, delay = _post(webhook, json.loads(row["first_embed"]))
            if outcome == "ok":
                _persist_message(db_path, message_id, webhook)
                _update(db_path, row, {"message_id": message_id,
                                       "updated_count": 1, "next_attempt_at": 0})
            elif outcome == "retry":
                _update(db_path, row, {"next_attempt_at": now + max(1, delay)})
            else:
                # The response might have been lost after Discord accepted it.
                # Delay uncertain retries rather than flooding on each tick.
                _update(db_path, row, {"next_attempt_at": now + 300})
            continue

        if row["updated_count"] < row["event_count"]:
            outcome, delay = _patch(webhook, row["message_id"], _session_embed(row))
            if outcome == "ok":
                _update(db_path, row, {"updated_count": row["event_count"],
                                       "next_attempt_at": 0})
            elif outcome == "missing":
                # The leading message could have been manually removed.
                _update(db_path, row, {"message_id": None,
                                       "first_embed": json.dumps(_session_embed(row)),
                                       "next_attempt_at": 0})
            else:
                _update(db_path, row, {"next_attempt_at": now + max(1, delay)})
            continue

        if now - row["last_event_at"] < quiet_seconds:
            continue

        outcome, message_id, delay = _post(webhook, _session_embed(row, final=True))
        if outcome == "ok":
            _persist_message(db_path, message_id, webhook)
            with sqlite3.connect(db_path, timeout=10) as conn:
                # A new fill arriving during POST leaves the session open.
                conn.execute("""
                    DELETE FROM notification_sessions
                    WHERE coin=? AND webhook_url=?
                      AND message_id=? AND event_count=?
                      AND last_event_at=?
                """, (row["coin"], webhook, row["message_id"],
                      row["event_count"], row["last_event_at"]))
        elif outcome == "retry":
            _update(db_path, row, {"next_attempt_at": now + max(1, delay)})
        else:
            _update(db_path, row, {"next_attempt_at": now + 300})


def dispatch_all(db_directory, quiet_seconds):
    for db_path in Path(db_directory).glob("trades_*.db"):
        try:
            dispatch_db(str(db_path), quiet_seconds)
        except (sqlite3.Error, OSError, ValueError):
            logger.exception("notification sweep failed db=%s", db_path.name)
