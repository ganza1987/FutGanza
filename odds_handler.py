"""
odds_handler.py

Captura y almacena cuotas de API-Football para los partidos de FutGanza.

Archivo NUEVO, independiente. No modifica analyzer.py, bot_handler.py,
bet_handler.py, image_bet_handler.py, dashboard.py ni build_database.py.
Solo depende de `database.py` (reutiliza get_conn/DB_TYPE, ya existentes)
y añade una tabla nueva (`cuotas`, ver 001_create_cuotas_table.sql).

USO TÍPICO (ver PATCH_INSTRUCTIONS.md para el enganche exacto en
scheduler.py): se llama una vez por partido, el mismo día que el bot ya
está analizando ese partido, porque el endpoint /odds de API-Football
solo retiene 7 días de histórico — no sirve para pedir cuotas de partidos
antiguos.

Nota importante: J1 League (liga_id API-Football 98) no tiene cobertura de
odds en API-Football (comprobado el 2026-08-17 contra /leagues). Por eso
está en LIGAS_SIN_ODDS: el resto del sistema (análisis, picks) sigue
funcionando igual para esa liga, simplemente no se intenta capturar cuota.
"""

import os
import json
import logging
import httpx

from database import get_conn, DB_TYPE

logger = logging.getLogger(__name__)

APIFOOTBALL_KEY = os.getenv("APIFOOTBALL_KEY", "")
APIFOOTBALL_URL = "https://v3.football.api-sports.io"

# IDs de liga (API-Football) sin cobertura de cuotas. Evita llamadas inútiles.
# Comprobado manualmente contra /leagues el 2026-08-17.
LIGAS_SIN_ODDS = {98}  # J1 League (Japón)


async def fetch_odds_for_fixture(fixture_id: int) -> list:
    """Llama a GET /odds?fixture=<id> y devuelve la lista `response` cruda
    de la API (puede estar vacía si aún no hay cuotas publicadas para ese
    partido, o si ya pasó la ventana de 7 días)."""
    headers = {
        "x-apisports-key": APIFOOTBALL_KEY,
        "x-rapidapi-host": "v3.football.api-sports.io",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{APIFOOTBALL_URL}/odds",
            headers=headers,
            params={"fixture": fixture_id},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("response", [])


def _save_odds_response(fixture_id: int, liga_id: int | None, response: list) -> int:
    """Guarda una fila por cada bookmaker presente en la respuesta.
    Devuelve cuántas filas se intentaron guardar (0 si no había datos)."""
    if not response:
        return 0

    conn = get_conn()
    cur = conn.cursor()
    saved = 0

    for entry in response:
        for bk in entry.get("bookmakers", []):
            bookmaker_id = bk.get("id")
            bookmaker_nombre = bk.get("name")
            markets_json = json.dumps(bk.get("bets", []))

            if DB_TYPE == "postgres":
                cur.execute(
                    """
                    INSERT INTO cuotas
                        (fixture_id, liga_id, bookmaker_id, bookmaker_nombre, markets)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (fixture_id, bookmaker_id, capturada_en) DO NOTHING
                    """,
                    (str(fixture_id), liga_id, bookmaker_id, bookmaker_nombre, markets_json),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO cuotas
                        (fixture_id, liga_id, bookmaker_id, bookmaker_nombre, markets)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(fixture_id), liga_id, bookmaker_id, bookmaker_nombre, markets_json),
                )
            saved += 1

    conn.commit()
    cur.close()
    conn.close()
    return saved


async def fetch_and_store_odds(fixture_id: int, liga_id: int | None = None) -> int:
    """Punto de entrada público. Descarga y guarda las cuotas de un partido
    si la liga tiene cobertura conocida. Nunca lanza excepción hacia fuera
    (solo registra el warning) para no romper el flujo del scheduler si la
    API de odds falla puntualmente."""
    if liga_id in LIGAS_SIN_ODDS:
        return 0
    try:
        response = await fetch_odds_for_fixture(fixture_id)
        saved = _save_odds_response(fixture_id, liga_id, response)
        if saved:
            logger.info(f"fetch_and_store_odds: {saved} bookmakers guardados para fixture {fixture_id}")
        return saved
    except Exception as e:
        logger.warning(f"fetch_and_store_odds({fixture_id}): {type(e).__name__}: {e}")
        return 0


# ── Utilidad de lectura para el futuro motor de backtesting ───────────────

# Nombres de mercado (campo "name" dentro de cada bet) más relevantes para
# comparar contra el modelo estadístico. Referencia rápida — no se usan
# todavía en este archivo, pero documentan qué buscar en `markets` (JSONB)
# cuando se construya el comparador modelo-vs-mercado.
MERCADOS_CLAVE = {
    "1x2": "Match Winner",
    "btts": "Both Teams Score",
    "over_under_goles": "Goals Over/Under",
    "over_under_corners": "Corners Over Under",
    "over_under_tarjetas": "Cards Over/Under",
}


def get_odds_for_fixture(fixture_id: int) -> list[dict]:
    """Lee de la BD (no llama a la API) todas las filas de cuotas guardadas
    para un fixture, ya parseadas. Cada elemento: {bookmaker_id,
    bookmaker_nombre, markets (lista de dicts), capturada_en}."""
    conn = get_conn()
    cur = conn.cursor()
    if DB_TYPE == "postgres":
        cur.execute(
            "SELECT bookmaker_id, bookmaker_nombre, markets, capturada_en "
            "FROM cuotas WHERE fixture_id=%s ORDER BY capturada_en DESC",
            (str(fixture_id),),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    else:
        cur.execute(
            "SELECT bookmaker_id, bookmaker_nombre, markets, capturada_en "
            "FROM cuotas WHERE fixture_id=? ORDER BY capturada_en DESC",
            (str(fixture_id),),
        )
        rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    for r in rows:
        if isinstance(r.get("markets"), str):
            r["markets"] = json.loads(r["markets"])
    return rows

