"""
PostgreSQL database for bet tracking.
Uses psycopg2 for persistent storage on Render.
"""
import os
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://futganza_db_user:Cx9MjEUwzUP1F95jvQcPhC2X5WTaTq5h@dpg-d8iq5ob7uimc73b2mlig-a/futganza_db"
)

try:
    import psycopg2
    import psycopg2.extras
    DB_TYPE = "postgres"
except ImportError:
    import sqlite3
    DB_TYPE = "sqlite"
    logger.warning("psycopg2 not available, falling back to SQLite")


def get_conn():
    if DB_TYPE == "postgres":
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        conn = sqlite3.connect("/tmp/futganza.db")
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id          SERIAL PRIMARY KEY,
                chat_id     TEXT NOT NULL,
                match       TEXT NOT NULL,
                match_date  TEXT,
                market      TEXT NOT NULL,
                odds        REAL,
                stake       REAL,
                result      TEXT DEFAULT 'pending',
                profit      REAL,
                analysis_id TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id         SERIAL PRIMARY KEY,
                chat_id    TEXT NOT NULL,
                match      TEXT NOT NULL,
                home       TEXT NOT NULL,
                away       TEXT NOT NULL,
                score      INTEGER,
                max_score  INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    else:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS bets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL,
                match       TEXT NOT NULL,
                match_date  TEXT,
                market      TEXT NOT NULL,
                odds        REAL,
                stake       REAL,
                result      TEXT DEFAULT 'pending',
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
    cur.close()
    conn.close()


def _fetchall(cur, query, params=()):
    cur.execute(query, params)
    if DB_TYPE == "postgres":
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    else:
        return [dict(r) for r in cur.fetchall()]


def _fetchone(cur, query, params=()):
    cur.execute(query, params)
    if DB_TYPE == "postgres":
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    else:
        row = cur.fetchone()
        return dict(row) if row else None


# ── Bets ──────────────────────────────────────────────────────────────────────

def add_bet(chat_id, match, market, odds=None, stake=None, match_date=None, analysis_id=None) -> int:
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute(
            "INSERT INTO bets (chat_id, match, match_date, market, odds, stake, analysis_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (str(chat_id), match, match_date, market, odds, stake, analysis_id)
        )
        bet_id = cur.fetchone()[0]
    else:
        cur.execute(
            "INSERT INTO bets (chat_id, match, match_date, market, odds, stake, analysis_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(chat_id), match, match_date, market, odds, stake, analysis_id)
        )
        bet_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return bet_id


def update_bet_result(bet_id: int, result: str):
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute("SELECT odds, stake FROM bets WHERE id=%s", (bet_id,))
        row = cur.fetchone()
    else:
        cur.execute("SELECT odds, stake FROM bets WHERE id=?", (bet_id,))
        row = cur.fetchone()

    profit = None
    if row:
        odds_val = row[0]
        stake_val = row[1]
        if stake_val:
            if result == "won":
                profit = round(stake_val * (odds_val - 1), 2) if odds_val else stake_val
            elif result == "lost":
                profit = -stake_val
            elif result == "void":
                profit = 0.0

    if DB_TYPE == "postgres":
        cur.execute("UPDATE bets SET result=%s, profit=%s WHERE id=%s", (result, profit, bet_id))
    else:
        cur.execute("UPDATE bets SET result=?, profit=? WHERE id=?", (result, profit, bet_id))

    conn.commit()
    cur.close()
    conn.close()


def get_bets(chat_id, limit=50, result_filter=None):
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        if result_filter:
            rows = _fetchall(cur, "SELECT * FROM bets WHERE chat_id=%s AND result=%s ORDER BY created_at DESC LIMIT %s",
                             (str(chat_id), result_filter, limit))
        else:
            rows = _fetchall(cur, "SELECT * FROM bets WHERE chat_id=%s ORDER BY created_at DESC LIMIT %s",
                             (str(chat_id), limit))
    else:
        if result_filter:
            rows = _fetchall(cur, "SELECT * FROM bets WHERE chat_id=? AND result=? ORDER BY created_at DESC LIMIT ?",
                             (str(chat_id), result_filter, limit))
        else:
            rows = _fetchall(cur, "SELECT * FROM bets WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                             (str(chat_id), limit))
    cur.close()
    conn.close()
    return rows


def get_stats(chat_id) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute(
            "SELECT result, COUNT(*) as n, SUM(stake) as staked, SUM(profit) as profit "
            "FROM bets WHERE chat_id=%s GROUP BY result", (str(chat_id),)
        )
    else:
        cur.execute(
            "SELECT result, COUNT(*) as n, SUM(stake) as staked, SUM(profit) as profit "
            "FROM bets WHERE chat_id=? GROUP BY result", (str(chat_id),)
        )

    stats = {"won": 0, "lost": 0, "pending": 0, "void": 0,
             "total_staked": 0.0, "total_profit": 0.0}
    for row in cur.fetchall():
        r = row[0]; n = row[1]; staked = row[2] or 0; profit = row[3] or 0
        stats[r] = n
        stats["total_staked"] += staked
        stats["total_profit"] += profit

    cur.close()
    conn.close()

    total_settled = stats["won"] + stats["lost"]
    stats["total_bets"] = total_settled + stats["pending"]
    stats["win_rate"] = round(stats["won"] / total_settled * 100, 1) if total_settled else 0
    stats["roi"] = round(stats["total_profit"] / stats["total_staked"] * 100, 1) if stats["total_staked"] else 0
    stats["total_profit"] = round(stats["total_profit"], 2)
    return stats


def get_bet_by_id(bet_id: int, chat_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        row = _fetchone(cur, "SELECT * FROM bets WHERE id=%s AND chat_id=%s", (bet_id, str(chat_id)))
    else:
        row = _fetchone(cur, "SELECT * FROM bets WHERE id=? AND chat_id=?", (bet_id, str(chat_id)))
    cur.close()
    conn.close()
    return row


def get_all_bets_web() -> list:
    conn = get_conn()
    cur = conn.cursor()
    rows = _fetchall(cur, "SELECT * FROM bets ORDER BY created_at DESC")
    cur.close()
    conn.close()
    return rows
