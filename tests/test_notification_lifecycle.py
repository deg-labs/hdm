import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from src import notification_lifecycle as lifecycle


def trade(direction="Open Long", tx="tx-1"):
    return SimpleNamespace(
        address="0x" + "1" * 40, coin="BTC", direction=direction,
        price=100.0, size=1.0, tx_hash=tx,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_leading_patch_and_trailing(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_11111111.db")
    sent, edited = [], []

    def post(url, embed):
        sent.append((url, embed))
        return "ok", str(len(sent)), 0

    def patch(url, message_id, embed):
        edited.append((url, message_id, embed))
        return "ok", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_patch", patch)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)

    lifecycle.enqueue(db, trade(), "trader", ["webhook-a", "webhook-a"],
                      {"title": "Open Long"}, now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    assert len(sent) == 1
    assert not edited

    lifecycle.enqueue(db, trade("Close Long", "tx-2"), "trader", ["webhook-a"],
                      {"title": "Close Long"}, now=110)
    lifecycle.dispatch_db(db, 60, now=111)
    assert len(sent) == 1
    assert len(edited) == 1
    assert edited[0][1] == "1"
    assert "2回" in edited[0][2]["description"]
    assert "Close Long" in edited[0][2]["description"]

    lifecycle.dispatch_db(db, 60, now=169)
    assert len(sent) == 1
    lifecycle.dispatch_db(db, 60, now=170)
    assert len(sent) == 2
    assert "変更完了" in sent[-1][1]["title"]
    assert "Close Long" in sent[-1][1]["description"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 0


def test_session_survives_restart_and_protects_active_message(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_22222222.db")
    sent = []

    def post(url, embed):
        sent.append(url)
        return "ok", str(len(sent)), 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one", "two"], {"title": "A"}, now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    with sqlite3.connect(db) as conn:
        assert lifecycle.active_message_ids(conn) == {"1", "2"}

    # No more fills arrive: the independent sweep still emits trailing POSTs.
    lifecycle.dispatch_db(db, 60, now=160)
    assert sent == ["one", "two", "one", "two"]
    with sqlite3.connect(db) as conn:
        assert lifecycle.active_message_ids(conn) == set()


def test_discord_retry_does_not_lose_last_event(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_33333333.db")
    responses = iter([("ok", "1", 0), ("retry", None, 5), ("ok", "2", 0)])
    monkeypatch.setattr(lifecycle, "_post", lambda *_: next(responses))
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one"], {"title": "A"}, now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.dispatch_db(db, 60, now=160)
    with sqlite3.connect(db) as conn:
        due = conn.execute("SELECT next_attempt_at FROM notification_sessions").fetchone()[0]
    assert due == 165
    lifecycle.dispatch_db(db, 60, now=165)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM notification_sessions").fetchone()[0] == 0


def test_unknown_leading_post_is_not_replayed(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_44444444.db")
    sent = []

    def post(url, embed):
        sent.append(embed["title"])
        if len(sent) == 1:
            return "unknown", None, 0
        return "ok", "999", 0

    monkeypatch.setattr(lifecycle, "_post", post)
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    assert sent == ["📈 BTC Open Long"]
    lifecycle.dispatch_db(db, 60, now=105)
    assert len(sent) == 1
    lifecycle.dispatch_db(db, 60, now=160)
    assert len(sent) == 2
    assert "変更完了" in sent[-1]


def test_patch_missing_restarts_leading_message(tmp_path, monkeypatch):
    db = str(tmp_path / "trades_55555555.db")
    sent = []
    monkeypatch.setattr(lifecycle, "_post", lambda url, embed: (
        sent.append(embed["title"]) or "ok", str(len(sent)), 0
    ))
    monkeypatch.setattr(lifecycle, "_patch", lambda *_: ("missing", 0))
    monkeypatch.setattr(lifecycle, "_persist_message", lambda *args: None)
    lifecycle.enqueue(db, trade(), None, ["one"], now=100)
    lifecycle.dispatch_db(db, 60, now=101)
    lifecycle.enqueue(db, trade("Close Long", "tx-2"), None, ["one"], now=110)
    lifecycle.dispatch_db(db, 60, now=111)
    lifecycle.dispatch_db(db, 60, now=112)
    assert len(sent) == 2
    assert "更新中" in sent[-1]
