import os
import httpx
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
APIFOOTBALL_KEY   = os.getenv("APIFOOTBALL_KEY", "888285a75737af52283245495c97c67a")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
APIFOOTBALL_URL   = "https://v3.football.api-sports.io"

DEFAULT_CONDITIONS = [
    {"id": "btts",           "label": "Ambos equipos marcan (BTTS)",                          "weight": 8},
    {"id": "over25",         "label": "Más de 2.5 goles en el partido",                        "weight": 7},
    {"id": "home_form",      "label": "El local tiene mejor forma reciente",                   "weight": 6},
    {"id": "away_goals",     "label": "Visitante promedia >1.5 goles/partido",                 "weight": 5},
    {"id": "clean_sheet",    "label": "Al menos un equipo con portería a 0 en últimos 3",      "weight": 4},
    {"id": "home_unbeaten",  "label": "Local invicto en sus últimos 5",                        "weight": 6},
    {"id": "h2h_goals",      "label": "H2H histórico: partidos con goles de ambos",            "weight": 5},
    {"id": "over85corners",  "label": "Más de 8.5 corners totales en el partido",              "weight": 6},
    {"id": "home_corners",   "label": "Local promedia más de 5 corners por partido en casa",   "weight": 5},
    {"id": "away_corners",   "label": "Visitante promedia más de 4 corners fuera de casa",     "weight": 4},
    {"id": "shots_home",     "label": "Local promedia más de 5 disparos a puerta en casa",     "weight": 5},
    {"id": "shots_away",     "label": "Visitante promedia más de 4 disparos a puerta fuera",   "weight": 4},
    {"id": "cards_over25",   "label": "Más de 2.5 tarjetas totales en el partido",             "weight": 5},
    {"id": "cards_home",     "label": "Local promedia más de 1.5 tarjetas por partido en casa","weight": 4},
    {"id": "cards_away",     "label": "Visitante promedia más de 2 tarjetas por partido fuera","weight": 4},
    {"id": "cards_h2h",      "label": "H2H histórico con más de 2 tarjetas por partido",       "weight": 3},
]

# ── API-Football ──────────────────────────────────────────────────────────────

async def apif(endpoint: str, params: dict) -> dict:
    headers = {
        "x-apisports-key": APIFOOTBALL_KEY,
        "x-rapidapi-host": "v3.football.api-sports.io",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{APIFOOTBALL_URL}/{endpoint}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()


async def find_team(name: str) -> dict | None:
    """Search team by name, return best match."""
    try:
        data = await apif("teams", {"search": name})
        results = data.get("response", [])
        if not results:
            return None
        # Prefer exact or close match
        name_lower = name.lower()
        for r in results:
            if name_lower in r["team"]["name"].lower():
                return r
        return results[0]
    except Exception as e:
        logger.warning(f"find_team({name}): {e}")
        return None


async def get_fixtures(team_id: int, last: int = 10) -> list:
    """Get last N fixtures for a team across all seasons."""
    try:
        data = await apif("fixtures", {"team": team_id, "last": last})
        return data.get("response", [])
    except Exception as e:
        logger.warning(f"get_fixtures({team_id}): {e}")
        return []


async def get_fixture_stats(fixture_id: int) -> list:
    """Get detailed stats for a fixture (corners, shots, cards)."""
    try:
        data = await apif("fixtures/statistics", {"fixture": fixture_id})
        return data.get("response", [])
    except Exception as e:
        logger.warning(f"get_fixture_stats({fixture_id}): {e}")
        return []


async def get_h2h(id1: int, id2: int, last: int = 6) -> list:
    try:
        data = await apif("fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": last})
        return data.get("response", [])
    except Exception as e:
        logger.warning(f"get_h2h({id1},{id2}): {e}")
        return []


def stat_value(stats: list, team_id: int, stat_name: str) -> str | None:
    for team_stats in stats:
        if team_stats.get("team", {}).get("id") == team_id:
            for s in team_stats.get("statistics", []):
                if s["type"] == stat_name:
                    return s["value"]
    return None


def result(fix: dict, team_id: int) -> str:
    home_id  = fix["teams"]["home"]["id"]
    gh = fix["goals"]["home"] or 0
    ga = fix["goals"]["away"] or 0
    gf = gh if home_id == team_id else ga
    gc = ga if home_id == team_id else gh
    return "W" if gf > gc else "D" if gf == gc else "L"


def format_fix(fix: dict, team_id: int, stats: list | None = None) -> str:
    date     = fix["fixture"]["date"][:10]
    home     = fix["teams"]["home"]["name"]
    away     = fix["teams"]["away"]["name"]
    gh       = fix["goals"]["home"]
    ga       = fix["goals"]["away"]
    score    = f"{gh}-{ga}" if gh is not None else "?-?"
    res      = result(fix, team_id) if gh is not None else "?"
    is_home  = fix["teams"]["home"]["id"] == team_id
    loc      = "🏠" if is_home else "✈️"

    extras = []
    if stats:
        corners_h = stat_value(stats, fix["teams"]["home"]["id"], "Corner Kicks")
        corners_a = stat_value(stats, fix["teams"]["away"]["id"], "Corner Kicks")
        shots_h   = stat_value(stats, fix["teams"]["home"]["id"], "Shots on Goal")
        shots_a   = stat_value(stats, fix["teams"]["away"]["id"], "Shots on Goal")
        ycard_h   = stat_value(stats, fix["teams"]["home"]["id"], "Yellow Cards")
        ycard_a   = stat_value(stats, fix["teams"]["away"]["id"], "Yellow Cards")
        rcard_h   = stat_value(stats, fix["teams"]["home"]["id"], "Red Cards")
        rcard_a   = stat_value(stats, fix["teams"]["away"]["id"], "Red Cards")

        if corners_h is not None and corners_a is not None:
            extras.append(f"corners {corners_h}-{corners_a}")
        if shots_h is not None and shots_a is not None:
            extras.append(f"disparos {shots_h}-{shots_a}")
        yh = ycard_h or 0; ya = ycard_a or 0
        rh = rcard_h or 0; ra = rcard_a or 0
        total_cards = int(yh) + int(ya) + int(rh) + int(ra)
        if total_cards > 0:
            extras.append(f"tarjetas {total_cards}")

    extras_str = f" [{', '.join(extras)}]" if extras else ""
    return f"{date} {loc} {home} {score} {away} ({res}){extras_str}"


def avg(values: list) -> float:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


# ── Build real data block ─────────────────────────────────────────────────────

async def build_real_data(home_name: str, away_name: str) -> tuple[str, bool]:
    """
    Returns (data_block_str, success_bool).
    Fetches last fixtures + stats for both teams + H2H.
    """
    home_team = await find_team(home_name)
    away_team = await find_team(away_name)

    if not home_team or not away_team:
        missing = []
        if not home_team: missing.append(home_name)
        if not away_team: missing.append(away_name)
        return f"⚠️ No se encontraron en API-Football: {', '.join(missing)}", False

    home_id   = home_team["team"]["id"]
    away_id   = away_team["team"]["id"]
    home_full = home_team["team"]["name"]
    away_full = away_team["team"]["name"]
    home_country = home_team.get("team", {}).get("country", "")
    away_country = away_team.get("team", {}).get("country", "")

    # Fetch fixtures
    home_fixes = await get_fixtures(home_id, 12)
    away_fixes = await get_fixtures(away_id, 12)
    h2h_fixes  = await get_h2h(home_id, away_id, 6)

    # Fetch stats for each fixture (last 8 each to save API calls)
    home_stats_map, away_stats_map = {}, {}
    for fix in home_fixes[:8]:
        fid = fix["fixture"]["id"]
        home_stats_map[fid] = await get_fixture_stats(fid)
    for fix in away_fixes[:8]:
        fid = fix["fixture"]["id"]
        if fid not in home_stats_map:
            away_stats_map[fid] = await get_fixture_stats(fid)

    # Separate home/away fixtures
    home_at_home = [f for f in home_fixes if f["teams"]["home"]["id"] == home_id]
    home_away    = [f for f in home_fixes if f["teams"]["away"]["id"] == home_id]
    away_at_away = [f for f in away_fixes if f["teams"]["away"]["id"] == away_id]
    away_at_home = [f for f in away_fixes if f["teams"]["home"]["id"] == away_id]

    # ── HOME team stats ──
    def team_stats_summary(fixes: list, team_id: int, stats_map: dict, label: str) -> str:
        lines = []
        gf_list, ga_list = [], []
        corners_list, shots_list, cards_list = [], [], []

        for fix in fixes[:6]:
            fid = fix["fixture"]["id"]
            stats = stats_map.get(fid, [])
            line = format_fix(fix, team_id, stats if stats else None)
            lines.append(f"  • {line}")

            gh = fix["goals"]["home"]
            ga = fix["goals"]["away"]
            if gh is not None and ga is not None:
                is_home = fix["teams"]["home"]["id"] == team_id
                gf_list.append(gh if is_home else ga)
                ga_list.append(ga if is_home else gh)

            if stats:
                home_id_fix = fix["teams"]["home"]["id"]
                away_id_fix = fix["teams"]["away"]["id"]
                c_h = stat_value(stats, home_id_fix, "Corner Kicks")
                c_a = stat_value(stats, away_id_fix, "Corner Kicks")
                s_h = stat_value(stats, home_id_fix, "Shots on Goal")
                s_a = stat_value(stats, away_id_fix, "Shots on Goal")
                y_h = stat_value(stats, home_id_fix, "Yellow Cards") or 0
                y_a = stat_value(stats, away_id_fix, "Yellow Cards") or 0
                r_h = stat_value(stats, home_id_fix, "Red Cards") or 0
                r_a = stat_value(stats, away_id_fix, "Red Cards") or 0

                if c_h is not None and c_a is not None:
                    corners_list.append(int(c_h if fix["teams"]["home"]["id"] == team_id else c_a))
                if s_h is not None and s_a is not None:
                    shots_list.append(int(s_h if fix["teams"]["home"]["id"] == team_id else s_a))
                total_cards = int(y_h) + int(y_a) + int(r_h) + int(r_a)
                if total_cards >= 0:
                    cards_list.append(total_cards)

        result_str = ""
        if lines:
            result_str += "\n".join(lines[:6])
        
        summary_parts = []
        if gf_list:
            summary_parts.append(f"⚽ Goles: {avg(gf_list)} marcados / {avg(ga_list)} encajados por partido")
        if corners_list:
            summary_parts.append(f"📐 Corners: {avg(corners_list)} por partido")
        else:
            summary_parts.append("📐 Corners: sin dato disponible")
        if shots_list:
            summary_parts.append(f"🎯 Disparos a puerta: {avg(shots_list)} por partido")
        else:
            summary_parts.append("🎯 Disparos a puerta: sin dato disponible")
        if cards_list:
            summary_parts.append(f"🟨 Tarjetas totales: {avg(cards_list)} por partido")
        else:
            summary_parts.append("🟨 Tarjetas: sin dato disponible")

        form = "".join([result(f, team_id) for f in fixes[:5] if f["goals"]["home"] is not None])
        summary_parts.append(f"📊 Forma reciente: {form or 'sin datos'}")

        return result_str + "\n" + "\n".join(summary_parts)

    home_home_block = team_stats_summary(home_at_home, home_id, home_stats_map, "casa")
    home_away_block = team_stats_summary(home_away, home_id, home_stats_map, "fuera")
    away_home_block = team_stats_summary(away_at_home, away_id, away_stats_map, "casa")
    away_away_block = team_stats_summary(away_at_away, away_id, away_stats_map, "fuera")

    # H2H
    h2h_lines = []
    h2h_goals, h2h_corners, h2h_cards = [], [], []
    for fix in h2h_fixes[:5]:
        fid = fix["fixture"]["id"]
        stats = home_stats_map.get(fid) or away_stats_map.get(fid) or []
        h2h_lines.append(f"  • {format_fix(fix, home_id, stats if stats else None)}")
        gh = fix["goals"]["home"]
        ga = fix["goals"]["away"]
        if gh is not None and ga is not None:
            h2h_goals.append(gh + ga)

    data_block = f"""
╔══════════════════════════════════════╗
  DATOS REALES — API-FOOTBALL
  {home_full} ({home_country}) vs {away_full} ({away_country})
  Extraídos: {datetime.now().strftime('%d/%m/%Y %H:%M')}
╚══════════════════════════════════════╝

🔵 {home_full.upper()} — EN CASA (últimos partidos como LOCAL):
{home_home_block}

🔵 {home_full.upper()} — DE VISITANTE (últimos partidos como VISITANTE):
{home_away_block}

🔴 {away_full.upper()} — EN CASA (últimos partidos como LOCAL):
{away_home_block}

🔴 {away_full.upper()} — DE VISITANTE (últimos partidos como VISITANTE):
{away_away_block}

⚔️ H2H — ÚLTIMOS ENFRENTAMIENTOS DIRECTOS:
{chr(10).join(h2h_lines) if h2h_lines else "  Sin datos H2H disponibles"}
{"  Goles medios por partido: " + str(avg(h2h_goals)) if h2h_goals else ""}
"""
    return data_block.strip(), True


# ── Prompt ────────────────────────────────────────────────────────────────────

def build_prompt(home: str, away: str, conditions: list[dict], data_block: str, api_ok: bool) -> str:
    conditions_block = "\n".join(
        f"  - [{c['id']}] {c['label']} (peso {c['weight']}/10)"
        for c in conditions
    )

    data_instruction = f"""
A continuación tienes los DATOS REALES extraídos de API-Football.
ÚSALOS como única fuente de verdad. NO los contradigas ni los ignores.
Si algún campo dice "sin dato disponible", indícalo igual en el análisis.

{data_block}
""" if api_ok else f"""
⚠️ No se pudieron obtener datos de API-Football. 
Usa la herramienta web_search para buscar los datos más recientes disponibles.
Indica claramente qué datos son aproximados.

{data_block}
"""

    return f"""Eres un analista deportivo experto en fútbol. Genera el análisis del partido *{home}* (local) vs *{away}* (visitante).

{data_instruction}

ESTRUCTURA OBLIGATORIA — usa exactamente estos bloques:

*⚽ {home.upper()} vs {away.upper()}*

*📋 1. CONTEXTO*
- Competición y contexto del partido

*🔵 2. {home.upper()} — TENDENCIAS*
_Como local:_
- Últimos resultados en casa con estadísticas
- Promedio goles / corners / disparos / tarjetas en casa

_Como visitante (contexto general):_
- Forma fuera de casa resumida

*🔴 3. {away.upper()} — TENDENCIAS*
_Como visitante:_
- Últimos resultados fuera con estadísticas
- Promedio goles / corners / disparos / tarjetas fuera

_Como local (contexto general):_
- Forma en casa resumida

*⚔️ 4. H2H DIRECTO*
- Enfrentamientos con estadísticas reales
- Goles, corners y tarjetas medias

*✅ 5. CONDICIONES ({len(conditions)} evaluadas)*
Para cada condición, basándote SOLO en los datos reales de arriba:
{conditions_block}

Tabla:
| Condición | Estado | Peso | Pts |

*📊 6. PUNTUACIÓN*
- XX / {sum(c['weight'] for c in conditions)} pts → XX%
- FAVORABLE / NEUTRO / DESFAVORABLE

*🔮 7. CONCLUSIÓN*
- Mercados avalados por datos reales: goles, corners, tarjetas

Formato Markdown Telegram. Máximo 4000 caracteres.
{"📡 _Fuente: API-Football (datos en tiempo real)_" if api_ok else "⚠️ _Fuente: búsqueda web (datos aproximados)_"}
"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def analyze_match(
    home: str,
    away: str,
    conditions: list[dict] | None = None
) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    # 1. Fetch real data from API-Football
    data_block, api_ok = await build_real_data(home, away)
    logger.info(f"API-Football data fetched (ok={api_ok}) for {home} vs {away}")

    # 2. Build prompt with real data
    prompt = build_prompt(home, away, conditions, data_block, api_ok)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    tools = []
    if not api_ok:
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]

    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 5000,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en fútbol. Respondes siempre en español. "
            "Cuando recibes datos reales de API-Football, los usas fielmente sin inventar nada. "
            "Cuando un dato no está disponible, lo indicas claramente. "
            "Tu formato es Markdown compatible con Telegram."
        ),
    }
    if tools:
        body["tools"] = tools

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            text_parts = [
                block["text"]
                for block in data.get("content", [])
                if block.get("type") == "text"
            ]
            return "\n".join(text_parts) if text_parts else "❌ No se pudo generar el análisis."
    except httpx.HTTPStatusError as e:
        logger.error(f"Anthropic API error: {e.response.text}")
        return "❌ Error al generar el análisis. Inténtalo de nuevo en unos segundos."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "❌ Error inesperado. Revisa los logs del servidor."
