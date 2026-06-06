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
    try:
        data = await apif("teams", {"search": name})
        results = data.get("response", [])
        if not results:
            return None
        name_lower = name.lower()
        for r in results:
            if name_lower in r["team"]["name"].lower():
                return r
        return results[0]
    except Exception as e:
        logger.warning(f"find_team({name}): {e}")
        return None


async def get_fixtures(team_id: int, last: int = 10) -> list:
    try:
        data = await apif("fixtures", {"team": team_id, "last": last})
        return data.get("response", [])
    except Exception as e:
        logger.warning(f"get_fixtures({team_id}): {e}")
        return []


async def get_fixture_stats(fixture_id: int) -> list:
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


def stat_value(stats: list, team_id: int, stat_name: str):
    for team_stats in stats:
        if team_stats.get("team", {}).get("id") == team_id:
            for s in team_stats.get("statistics", []):
                if s["type"] == stat_name:
                    return s["value"]
    return None


def get_result(fix: dict, team_id: int) -> str:
    home_id = fix["teams"]["home"]["id"]
    gh = fix["goals"]["home"] or 0
    ga = fix["goals"]["away"] or 0
    gf = gh if home_id == team_id else ga
    gc = ga if home_id == team_id else gh
    return "W" if gf > gc else "D" if gf == gc else "L"


def avg(values: list) -> float:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def format_fix_with_stats(fix: dict, team_id: int, stats: list) -> tuple[str, dict]:
    date    = fix["fixture"]["date"][:10]
    home    = fix["teams"]["home"]["name"]
    away    = fix["teams"]["away"]["name"]
    gh      = fix["goals"]["home"]
    ga      = fix["goals"]["away"]
    score   = f"{gh}-{ga}" if gh is not None else "?-?"
    res     = get_result(fix, team_id) if gh is not None else "?"
    is_home = fix["teams"]["home"]["id"] == team_id
    loc     = "🏠" if is_home else "✈️"

    stat_data = {}
    extras = []
    if stats:
        h_id = fix["teams"]["home"]["id"]
        a_id = fix["teams"]["away"]["id"]
        c_h  = stat_value(stats, h_id, "Corner Kicks")
        c_a  = stat_value(stats, a_id, "Corner Kicks")
        s_h  = stat_value(stats, h_id, "Shots on Goal")
        s_a  = stat_value(stats, a_id, "Shots on Goal")
        y_h  = int(stat_value(stats, h_id, "Yellow Cards") or 0)
        y_a  = int(stat_value(stats, a_id, "Yellow Cards") or 0)
        r_h  = int(stat_value(stats, h_id, "Red Cards") or 0)
        r_a  = int(stat_value(stats, a_id, "Red Cards") or 0)

        team_corners = int(c_h if is_home else c_a) if (c_h is not None and c_a is not None) else None
        team_shots   = int(s_h if is_home else s_a) if (s_h is not None and s_a is not None) else None
        total_cards  = y_h + y_a + r_h + r_a

        if team_corners is not None:
            extras.append(f"corners:{team_corners}")
            stat_data["corners"] = team_corners
        if team_shots is not None:
            extras.append(f"disp:{team_shots}")
            stat_data["shots"] = team_shots
        if total_cards > 0:
            extras.append(f"tarj:{total_cards}")
            stat_data["cards"] = total_cards

    extras_str = f" [{', '.join(extras)}]" if extras else ""
    line = f"{date} {loc} {home} {score} {away} ({res}){extras_str}"
    return line, stat_data


async def build_team_block(team_id: int, team_name: str) -> tuple[str, dict]:
    """Build stats block for a team. Returns (text_block, aggregated_stats)."""
    fixtures = await get_fixtures(team_id, 12)
    if not fixtures:
        return f"Sin datos disponibles para {team_name}", {}

    home_fixes = [f for f in fixtures if f["teams"]["home"]["id"] == team_id]
    away_fixes = [f for f in fixtures if f["teams"]["away"]["id"] == team_id]

    agg = {"home": {"gf":[],"ga":[],"corners":[],"shots":[],"cards":[]},
           "away": {"gf":[],"ga":[],"corners":[],"shots":[],"cards":[]}}

    async def process_fixes(fixes, loc_key):
        lines = []
        for fix in fixes[:6]:
            fid   = fix["fixture"]["id"]
            stats = await get_fixture_stats(fid)
            line, sd = format_fix_with_stats(fix, team_id, stats)
            lines.append(f"  • {line}")
            gh = fix["goals"]["home"]
            ga = fix["goals"]["away"]
            if gh is not None and ga is not None:
                is_home = fix["teams"]["home"]["id"] == team_id
                agg[loc_key]["gf"].append(gh if is_home else ga)
                agg[loc_key]["ga"].append(ga if is_home else gh)
            for k in ["corners","shots","cards"]:
                if k in sd:
                    agg[loc_key][k].append(sd[k])
        return lines

    home_lines = await process_fixes(home_fixes, "home")
    away_lines = await process_fixes(away_fixes, "away")

    def summary(loc_key, label):
        d = agg[loc_key]
        gf  = f"{avg(d['gf'])} marcados / {avg(d['ga'])} encajados" if d["gf"] else "sin dato"
        cor = f"{avg(d['corners'])} por partido" if d["corners"] else "sin dato disponible"
        sho = f"{avg(d['shots'])} por partido"   if d["shots"]   else "sin dato disponible"
        car = f"{avg(d['cards'])} por partido"   if d["cards"]   else "sin dato disponible"
        form_fixes = home_fixes if loc_key=="home" else away_fixes
        form = "".join([get_result(f, team_id) for f in form_fixes[:5] if f["goals"]["home"] is not None])
        return (
            f"_Forma ({label}): {form or 'sin datos'}_\n"
            f"⚽ Goles: {gf}\n"
            f"📐 Corners: {cor}\n"
            f"🎯 Disparos a puerta: {sho}\n"
            f"🟨 Tarjetas: {car}"
        )

    block = (
        f"*En casa:*\n" + ("\n".join(home_lines) if home_lines else "  Sin datos") + "\n" +
        summary("home", "local") + "\n\n" +
        f"*De visitante:*\n" + ("\n".join(away_lines) if away_lines else "  Sin datos") + "\n" +
        summary("away", "visitante")
    )
    return block, agg


async def build_real_data(home_name: str, away_name: str) -> tuple[str, bool]:
    home_team = await find_team(home_name)
    away_team = await find_team(away_name)

    found_home = home_team is not None
    found_away = away_team is not None

    if not found_home and not found_away:
        return "", False

    lines = [f"╔ DATOS REALES — API-Football ({datetime.now().strftime('%d/%m/%Y %H:%M')}) ╗\n"]

    if found_home:
        home_id   = home_team["team"]["id"]
        home_full = home_team["team"]["name"]
        lines.append(f"🔵 *{home_full.upper()}*")
        block, _ = await build_team_block(home_id, home_full)
        lines.append(block)
    else:
        lines.append(f"🔵 *{home_name.upper()}* — No encontrado en API-Football")

    if found_away:
        away_id   = away_team["team"]["id"]
        away_full = away_team["team"]["name"]
        lines.append(f"\n🔴 *{away_full.upper()}*")
        block, _ = await build_team_block(away_id, away_full)
        lines.append(block)
    else:
        lines.append(f"\n🔴 *{away_name.upper()}* — No encontrado en API-Football")

    if found_home and found_away:
        h2h = await get_h2h(home_team["team"]["id"], away_team["team"]["id"], 6)
        lines.append("\n⚔️ *H2H DIRECTO:*")
        if h2h:
            for fix in h2h[:5]:
                fid   = fix["fixture"]["id"]
                stats = await get_fixture_stats(fid)
                line, _ = format_fix_with_stats(fix, home_team["team"]["id"], stats)
                lines.append(f"  • {line}")
        else:
            lines.append("  Sin datos H2H disponibles")

    api_ok = found_home or found_away
    return "\n".join(lines), api_ok


def build_prompt(home: str, away: str, conditions: list[dict], data_block: str, api_ok: bool) -> str:
    now = datetime.now()
    current_date = now.strftime("%d/%m/%Y")
    conditions_block = "\n".join(
        f"  - [{c['id']}] {c['label']} (peso {c['weight']}/10)"
        for c in conditions
    )
    max_pts = sum(c['weight'] for c in conditions)

    if api_ok:
        data_section = f"""
Los siguientes datos son REALES extraídos de API-Football ahora mismo ({current_date}).
Úsalos como fuente principal. NO los contradigas.
Si algún campo dice "sin dato disponible", indícalo igual en el análisis.

{data_block}

IMPORTANTE: Para los equipos o estadísticas NO encontrados en API-Football,
usa la herramienta web_search buscando en Sofascore o Flashscore:
- "site:sofascore.com {home} estadísticas 2026"
- "site:sofascore.com {away} estadísticas 2026"
- "{home} {away} flashscore head to head"
"""
    else:
        data_section = f"""
API-Football no tiene datos de estos equipos (probablemente liga regional).
DEBES usar web_search para buscar en Sofascore y Flashscore:
1. "site:sofascore.com {home} resultados 2026"
2. "site:sofascore.com {away} resultados 2026"
3. "{home} {away} sofascore head to head"
4. "{home} corners tarjetas estadísticas sofascore"
5. "{away} corners tarjetas estadísticas sofascore"
6. "flashscore {home} {away}"

Usa ÚNICAMENTE los datos encontrados. Si no encuentras un dato, escribe "sin dato disponible".
"""

    return f"""Eres un analista deportivo experto. Genera el análisis del partido *{home}* (local) vs *{away}* (visitante). Hoy: {current_date}.

{data_section}

ESTRUCTURA OBLIGATORIA:

*⚽ {home.upper()} vs {away.upper()}*

*📋 1. CONTEXTO*
- Competición, jornada y clasificación actual

*🔵 2. {home.upper()} — TENDENCIAS*
_Como local (últimos partidos en casa):_
- Resultados con fecha, marcador y estadísticas (corners, disparos, tarjetas)
- ⚽ Promedio goles en casa
- 📐 Corners en casa
- 🎯 Disparos a puerta en casa
- 🟨 Tarjetas en casa

_Como visitante (resumen):_
- Forma y promedios fuera de casa

*🔴 3. {away.upper()} — TENDENCIAS*
_Como visitante (últimos partidos fuera):_
- Resultados con fecha, marcador y estadísticas
- ⚽ Promedio goles fuera
- 📐 Corners fuera
- 🎯 Disparos a puerta fuera
- 🟨 Tarjetas fuera

_Como local (resumen):_
- Forma y promedios en casa

*⚔️ 4. H2H DIRECTO*
- Últimos enfrentamientos con estadísticas reales
- Goles, corners y tarjetas medias

*✅ 5. CONDICIONES ({len(conditions)} evaluadas)*
Evalúa cada condición con los datos reales:
{conditions_block}

Tabla resumen:
| Condición | ✅/❌ | Peso | Pts |

*📊 6. PUNTUACIÓN GLOBAL*
- Puntos obtenidos / {max_pts} máximos → XX%
- FAVORABLE (>60%) / NEUTRO (40-60%) / DESFAVORABLE (<40%)

*🔮 7. CONCLUSIÓN*
- Mercados avalados: goles, corners, tarjetas

Formato Markdown Telegram. Máximo 4000 caracteres.
{"📡 _Fuente: API-Football + Sofascore_" if api_ok else "📡 _Fuente: Sofascore / Flashscore (búsqueda web)_"}
"""


async def analyze_match(home: str, away: str, conditions: list[dict] | None = None) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    data_block, api_ok = await build_real_data(home, away)
    logger.info(f"API-Football ok={api_ok} for {home} vs {away}")

    prompt = build_prompt(home, away, conditions, data_block, api_ok)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 5000,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en fútbol. Respondes siempre en español. "
            "Cuando recibes datos reales de API-Football, los usas fielmente. "
            "Para datos no disponibles en API-Football, usas web_search buscando en Sofascore y Flashscore. "
            "NUNCA inventas estadísticas. Si no encuentras un dato lo indicas claramente. "
            "Formato Markdown compatible con Telegram."
        ),
    }

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
