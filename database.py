"""
PostgreSQL database for bet tracking.
Uses psycopg2 for persistent storage on Render.
Shared by FutGanza (sport='football') and HandGanza (sport='handball').
"""
import os
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

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
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL no configurada")
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
                sport       TEXT DEFAULT 'football',
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
                sport      TEXT DEFAULT 'football',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Añadir columna sport si ya existe la tabla sin ella
        cur.execute("""
            ALTER TABLE bets ADD COLUMN IF NOT EXISTS sport TEXT DEFAULT 'football';
        """)
        cur.execute("""
            ALTER TABLE analyses ADD COLUMN IF NOT EXISTS sport TEXT DEFAULT 'football';
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS picks_historial (
                id             SERIAL PRIMARY KEY,
                fecha          TEXT NOT NULL,
                liga           TEXT NOT NULL,
                sport          TEXT DEFAULT 'football',
                equipo_local   TEXT NOT NULL,
                equipo_visitante TEXT NOT NULL,
                fixture_id     TEXT,
                kickoff        TEXT,
                condicion_id   TEXT NOT NULL,
                condicion_label TEXT NOT NULL,
                probabilidad   INTEGER NOT NULL,
                muestra        TEXT,
                resultado      TEXT DEFAULT 'pending',
                verificado_en  TIMESTAMP,
                created_at     TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cuotas (
                id               SERIAL PRIMARY KEY,
                fixture_id       TEXT NOT NULL,
                liga_id          INT,
                bookmaker_id     INT,
                bookmaker_nombre TEXT,
                markets          JSONB,
                capturada_en     TIMESTAMP DEFAULT NOW(),
                UNIQUE(fixture_id, bookmaker_id, capturada_en)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cuotas_fixture_id ON cuotas (fixture_id)
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
                sport       TEXT DEFAULT 'football',
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
                sport      TEXT DEFAULT 'football',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS picks_historial (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha            TEXT NOT NULL,
                liga             TEXT NOT NULL,
                sport            TEXT DEFAULT 'football',
                equipo_local     TEXT NOT NULL,
                equipo_visitante TEXT NOT NULL,
                fixture_id       TEXT,
                kickoff          TEXT,
                condicion_id     TEXT NOT NULL,
                condicion_label  TEXT NOT NULL,
                probabilidad     INTEGER NOT NULL,
                muestra          TEXT,
                resultado        TEXT DEFAULT 'pending',
                verificado_en    TEXT,
                created_at       TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS cuotas (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id       TEXT NOT NULL,
                liga_id          INT,
                bookmaker_id     INT,
                bookmaker_nombre TEXT,
                markets          TEXT,
                capturada_en     TEXT DEFAULT (datetime('now'))
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

def add_bet(chat_id, match, market, odds=None, stake=None, match_date=None, analysis_id=None, sport="football") -> int:
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute(
            "INSERT INTO bets (chat_id, match, match_date, market, odds, stake, analysis_id, sport) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (str(chat_id), match, match_date, market, odds, stake, analysis_id, sport)
        )
        bet_id = cur.fetchone()[0]
    else:
        cur.execute(
            "INSERT INTO bets (chat_id, match, match_date, market, odds, stake, analysis_id, sport) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(chat_id), match, match_date, market, odds, stake, analysis_id, sport)
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


def get_bets(chat_id, limit=50, result_filter=None, sport=None):
    conn = get_conn()
    cur = conn.cursor()

    if DB_TYPE == "postgres":
        if result_filter and sport:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=%s AND result=%s AND sport=%s ORDER BY created_at DESC LIMIT %s",
                (str(chat_id), result_filter, sport, limit))
        elif result_filter:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=%s AND result=%s ORDER BY created_at DESC LIMIT %s",
                (str(chat_id), result_filter, limit))
        elif sport:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=%s AND sport=%s ORDER BY created_at DESC LIMIT %s",
                (str(chat_id), sport, limit))
        else:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=%s ORDER BY created_at DESC LIMIT %s",
                (str(chat_id), limit))
    else:
        if result_filter and sport:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=? AND result=? AND sport=? ORDER BY created_at DESC LIMIT ?",
                (str(chat_id), result_filter, sport, limit))
        elif result_filter:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=? AND result=? ORDER BY created_at DESC LIMIT ?",
                (str(chat_id), result_filter, limit))
        elif sport:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=? AND sport=? ORDER BY created_at DESC LIMIT ?",
                (str(chat_id), sport, limit))
        else:
            rows = _fetchall(cur,
                "SELECT * FROM bets WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                (str(chat_id), limit))

    cur.close()
    conn.close()
    return rows


def get_stats(chat_id, sport=None) -> dict:
    conn = get_conn()
    cur = conn.cursor()

    if DB_TYPE == "postgres":
        if sport:
            cur.execute(
                "SELECT result, COUNT(*) as n, SUM(stake) as staked, SUM(profit) as profit "
                "FROM bets WHERE chat_id=%s AND sport=%s GROUP BY result",
                (str(chat_id), sport)
            )
        else:
            cur.execute(
                "SELECT result, COUNT(*) as n, SUM(stake) as staked, SUM(profit) as profit "
                "FROM bets WHERE chat_id=%s GROUP BY result", (str(chat_id),)
            )
    else:
        if sport:
            cur.execute(
                "SELECT result, COUNT(*) as n, SUM(stake) as staked, SUM(profit) as profit "
                "FROM bets WHERE chat_id=? AND sport=? GROUP BY result",
                (str(chat_id), sport)
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


def get_all_bets_web(sport=None) -> list:
    conn = get_conn()
    cur = conn.cursor()
    if sport:
        if DB_TYPE == "postgres":
            rows = _fetchall(cur, "SELECT * FROM bets WHERE sport=%s ORDER BY created_at DESC", (sport,))
        else:
            rows = _fetchall(cur, "SELECT * FROM bets WHERE sport=? ORDER BY created_at DESC", (sport,))
    else:
        rows = _fetchall(cur, "SELECT * FROM bets ORDER BY created_at DESC")
    cur.close()
    conn.close()
    return rows


# ── Picks (backtesting de precision) ───────────────────────────────────────────

def add_pick(fecha, liga, equipo_local, equipo_visitante, fixture_id, kickoff,
             condicion_id, condicion_label, probabilidad, muestra=None, sport="football") -> int:
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute(
            "INSERT INTO picks_historial "
            "(fecha, liga, sport, equipo_local, equipo_visitante, fixture_id, kickoff, "
            " condicion_id, condicion_label, probabilidad, muestra) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (fecha, liga, sport, equipo_local, equipo_visitante, str(fixture_id) if fixture_id else None,
             kickoff, condicion_id, condicion_label, probabilidad, muestra)
        )
        pick_id = cur.fetchone()[0]
    else:
        cur.execute(
            "INSERT INTO picks_historial "
            "(fecha, liga, sport, equipo_local, equipo_visitante, fixture_id, kickoff, "
            " condicion_id, condicion_label, probabilidad, muestra) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fecha, liga, sport, equipo_local, equipo_visitante, str(fixture_id) if fixture_id else None,
             kickoff, condicion_id, condicion_label, probabilidad, muestra)
        )
        pick_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return pick_id


def get_pending_picks_to_verify(older_than_hours: int = 3, sport="football") -> list:
    """Devuelve los picks pendientes cuyo kickoff ya paso hace mas de X horas
    (tiempo suficiente para que el partido haya terminado), agrupables por
    fixture_id para no repetir la misma consulta a la API por partido."""
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        rows = _fetchall(cur,
            "SELECT * FROM picks_historial WHERE resultado='pending' AND sport=%s "
            "AND fixture_id IS NOT NULL AND kickoff IS NOT NULL "
            "AND kickoff::timestamptz < NOW() - (%s || ' hours')::interval",
            (sport, older_than_hours))
    else:
        rows = _fetchall(cur,
            "SELECT * FROM picks_historial WHERE resultado='pending' AND sport=? "
            "AND fixture_id IS NOT NULL AND kickoff IS NOT NULL "
            "AND datetime(kickoff) < datetime('now', ?)",
            (sport, f"-{older_than_hours} hours"))
    cur.close()
    conn.close()
    return rows


def update_pick_result(pick_id: int, resultado: str):
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute(
            "UPDATE picks_historial SET resultado=%s, verificado_en=NOW() WHERE id=%s",
            (resultado, pick_id)
        )
    else:
        cur.execute(
            "UPDATE picks_historial SET resultado=?, verificado_en=datetime('now') WHERE id=?",
            (resultado, pick_id)
        )
    conn.commit()
    cur.close()
    conn.close()


def get_picks_accuracy_stats(sport="football") -> dict:
    """Calcula el % de acierto global, por tipo de condicion y por rango de
    confianza, usando solo los picks ya verificados (hit/miss)."""
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        rows = _fetchall(cur,
            "SELECT condicion_id, condicion_label, probabilidad, resultado "
            "FROM picks_historial WHERE sport=%s AND resultado IN ('hit','miss')",
            (sport,))
    else:
        rows = _fetchall(cur,
            "SELECT condicion_id, condicion_label, probabilidad, resultado "
            "FROM picks_historial WHERE sport=? AND resultado IN ('hit','miss')",
            (sport,))
    cur.close()
    conn.close()

    def _bucket(prob):
        if prob >= 90: return "90-100%"
        if prob >= 80: return "80-89%"
        return "70-79%"

    total_hit = sum(1 for r in rows if r["resultado"] == "hit")
    total = len(rows)

    por_condicion = {}
    por_rango = {}
    for r in rows:
        cid = r["condicion_id"]
        label = r["condicion_label"]
        hit = 1 if r["resultado"] == "hit" else 0

        c = por_condicion.setdefault(cid, {"label": label, "hit": 0, "total": 0})
        c["hit"] += hit
        c["total"] += 1

        bucket = _bucket(r["probabilidad"])
        b = por_rango.setdefault(bucket, {"hit": 0, "total": 0})
        b["hit"] += hit
        b["total"] += 1

    return {
        "total": total,
        "total_hit": total_hit,
        "win_rate": round(total_hit / total * 100, 1) if total else 0,
        "por_condicion": por_condicion,
        "por_rango": por_rango,
    }
