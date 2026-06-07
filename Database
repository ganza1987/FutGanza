"""
Simple SQLite database for bet tracking.
"""
import sqlite3
import os
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "/tmp/futganza.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     TEXT NOT NULL,
        match       TEXT NOT NULL,
        match_date  TEXT,
        market      TEXT NOT NULL,
        odds        REAL,
        stake       REAL,
        result      TEXT DEFAULT 'pending',  -- pending / won / lost / void
        profit      REAL,
        analysis_id TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS analyses (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    TEXT NOT NULL,
        match      TEXT NOT NULL,
        home       TEXT NOT NULL,
        away       TEXT NOT NULL,
        score      INTEGER,
        max_score  INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()


# ── Bets ──────────────────────────────────────────────────────────────────────

def add_bet(chat_id, match, market, odds=None, stake=None, match_date=None, analysis_id=None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO bets (chat_id, match, match_date, market, odds, stake, analysis_id) VALUES (?,?,?,?,?,?,?)",
        (str(chat_id), match, match_date, market, odds, stake, analysis_id)
    )
    bet_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def update_bet_result(bet_id: int, result: str):
    """result: won / lost / void"""
    conn = get_conn()
    bet = conn.execute("SELECT odds, stake FROM bets WHERE id=?", (bet_id,)).fetchone()
    profit = None
    if bet and bet["stake"]:
        if result == "won":
            profit = round(bet["stake"] * (bet["odds"] - 1), 2) if bet["odds"] else bet["stake"]
        elif result == "lost":
            profit = -bet["stake"]
        elif result == "void":
            profit = 0.0
    conn.execute(
        "UPDATE bets SET result=?, profit=? WHERE id=?",
        (result, profit, bet_id)
    )
    conn.commit()
    conn.close()


def get_bets(chat_id, limit=50, result_filter=None):
    conn = get_conn()
    if result_filter:
        rows = conn.execute(
            "SELECT * FROM bets WHERE chat_id=? AND result=? ORDER BY created_at DESC LIMIT ?",
            (str(chat_id), result_filter, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bets WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (str(chat_id), limit)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats(chat_id) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT result, COUNT(*) as n, SUM(stake) as staked, SUM(profit) as profit "
        "FROM bets WHERE chat_id=? GROUP BY result",
        (str(chat_id),)
    ).fetchall()
    conn.close()

    stats = {"won": 0, "lost": 0, "pending": 0, "void": 0,
             "total_staked": 0.0, "total_profit": 0.0, "roi": 0.0}
    for r in rows:
        stats[r["result"]] = r["n"]
        if r["staked"]:
            stats["total_staked"] += r["staked"]
        if r["profit"]:
            stats["total_profit"] += r["profit"]

    total_settled = stats["won"] + stats["lost"]
    stats["total_bets"] = total_settled + stats["pending"]
    stats["win_rate"] = round(stats["won"] / total_settled * 100, 1) if total_settled else 0
    stats["roi"] = round(stats["total_profit"] / stats["total_staked"] * 100, 1) if stats["total_staked"] else 0
    stats["total_profit"] = round(stats["total_profit"], 2)
    return stats


def get_bet_by_id(bet_id: int, chat_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM bets WHERE id=? AND chat_id=?", (bet_id, str(chat_id))
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_bets_web() -> list:
    """For web dashboard — returns all bets."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bets ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
