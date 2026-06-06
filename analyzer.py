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
    {"id": "h2h_goals",      "label": "H2H: ambos equipos marcan",                            "weight": 5},
    {"id": "over85corners",  "label": "Más de 8.5 corners totales",                            "weight": 6},
    {"id": "home_corners",   "label": "Local >5 corners en casa",                              "weight": 5},
    {"id": "away_corners",   "label": "Visitante >4 corners fuera",                            "weight": 4},
    {"id": "shots_home",     "label": "Local >5 disparos a puerta en casa",                    "weight": 5},
    {"id": "shots_away",     "label": "Visitante >4 disparos a puerta fuera",                  "weight": 4},
    {"id": "cards_over25",   "label": "Más de 2.5 tarjetas totales",                           "weight": 5},
    {"id": "cards_home",     "label": "Local >1.5 tarjetas en casa",                           "weight": 4},
    {"id": "cards_away",     "label": "Visitante >2 tarjetas fuera",                           "weight": 4},
    {"id": "cards_h2h",      "label": "H2H con >2 tarjetas por partido",                       "weight": 3},
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
        for r in results:
            if name.lower() in r["team"]["name"].lower():
                return r
        return results[0]
    except Exception as e:
        logger.warning(f"find_team({name}): {e}")
        return None

async def get_fixtures(team_id: int, last: int = 12) -> list:
    try:
        data = await apif("fixtures", {"team": team_id, "last": last})
        return data.get("response", [])
    except Exception as e:
        logger.warning(f"get_fixtures: {e}")
        return []

async def get_fixture_stats(fixture_id: int) -> list:
    try:
        data = await apif("fixtures/statistics", {"fixture": fixture_id})
        return data.get("response", [])
    except Exception as e:
        return []

async def get_h2h(id1: int, id2: int) -> list:
    try:
        data = await apif("fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": 6})
        return data.get("response", [])
    except Exception as e:
        return []

def sv(stats, team_id, name):
    for t in stats:
        if t.get("team", {}).get("id") == team_id:
            for s in t.get("statistics", []):
                if s["type"] == name:
                    v = s["value"]
                    return int(v) if v is not None else None
    return None

def avg(vals): 
    v = [x for x in vals if x is not None]
    return round(sum(v)/len(v), 1) if v else None

def res(fix, team_id):
    gh = fix["goals"]["home"] or 0
    ga = fix["goals"]["away"] or 0
    is_home = fix["teams"]["home"]["id"] == team_id
    gf = gh if is_home else ga
    gc = ga if is_home else gh
    return "W" if gf > gc else "D" if gf == gc else "L"

def fmt_result(fix, team_id):
    is_home = fix["teams"]["home"]["id"] == team_id
    gh = fix["goals"]["home"]
    ga = fix["goals"]["away"]
    if gh is None: return "?"
    r = res(fix, team_id)
    emoji = "✅" if r == "W" else "🟡" if r == "D" else "❌"
    opp = fix["teams"]["away"]["name"] if is_home else fix["teams"]["home"]["name"]
    return f"{emoji}{gh}-{ga} {opp[:8]}"

async def team_data(team_id: int) -> dict:
    fixes = await get_fixtures(team_id, 12)
    home_fixes = [f for f in fixes if f["teams"]["home"]["id"] == team_id and f["goals"]["home"] is not None]
    away_fixes = [f for f in fixes if f["teams"]["away"]["id"] == team_id and f["goals"]["home"] is not None]

    async def calc(fix_list, loc):
        gf_l, ga_l, cor_l, sho_l, car_l = [], [], [], [], []
        results = []
        for fix in fix_list[:6]:
            stats = await get_fixture_stats(fix["fixture"]["id"])
            h_id = fix["teams"]["home"]["id"]
            a_id = fix["teams"]["away"]["id"]
            is_h = h_id == team_id
            gh = fix["goals"]["home"] or 0
            ga = fix["goals"]["away"] or 0
            gf_l.append(gh if is_h else ga)
            ga_l.append(ga if is_h else gh)
            results.append(res(fix, team_id))
            if stats:
                c = sv(stats, h_id if is_h else a_id, "Corner Kicks")
                s = sv(stats, h_id if is_h else a_id, "Shots on Goal")
                yh = sv(stats, h_id, "Yellow Cards") or 0
                ya = sv(stats, a_id, "Yellow Cards") or 0
                rh = sv(stats, h_id, "Red Cards") or 0
                ra = sv(stats, a_id, "Red Cards") or 0
                if c is not None: cor_l.append(c)
                if s is not None: sho_l.append(s)
                car_l.append(yh + ya + rh + ra)
        return {
            "results": results,
            "form": "".join(results[:5]),
            "gf": avg(gf_l), "ga": avg(ga_l),
            "corners": avg(cor_l), "shots": avg(sho_l), "cards": avg(car_l),
            "fixes": fix_list[:5]
        }

    return {
        "home": await calc(home_fixes, "home"),
        "away": await calc(away_fixes, "away"),
        "all_fixes": fixes
    }

async def build_real_data(home_name, away_name):
    ht = await find_team(home_name)
    at = await find_team(away_name)
    api_ok = ht is not None or at is not None

    data = {"home_team": ht, "away_team": at, "api_ok": api_ok,
            "home_data": None, "away_data": None, "h2h": []}

    if ht:
        data["home_data"] = await team_data(ht["team"]["id"])
    if at:
        data["away_data"] = await team_data(at["team"]["id"])
    if ht and at:
        data["h2h"] = await get_h2h(ht["team"]["id"], at["team"]["id"])

    return data

def nd(val, suffix=""):
    return f"{val}{suffix}" if val is not None else "s/d"

def build_prompt(home: str, away: str, conditions: list[dict], data: dict) -> str:
    now = datetime.now().strftime("%d/%m/%Y")
    max_pts = sum(c["weight"] for c in conditions)
    conditions_block = "\n".join(
        f'  [{c["id"]}] {c["label"]} — peso {c["weight"]}pts'
        for c in conditions
    )

    # Build data block
    blocks = [f"=== DATOS REALES ({now}) ===\n"]

    if data["home_data"]:
        hd = data["home_data"]
        hn = data["home_team"]["team"]["name"]
        home_results = " · ".join([fmt_result(f, data["home_team"]["team"]["id"]) for f in hd["home"]["fixes"]])
        away_results = " · ".join([fmt_result(f, data["home_team"]["team"]["id"]) for f in hd["away"]["fixes"]])
        blocks.append(
            f"🔵 {hn.upper()}\n"
            f"  En casa: {home_results}\n"
            f"  Forma casa: {hd['home']['form']} | Goles: {nd(hd['home']['gf'])} marc / {nd(hd['home']['ga'])} enc\n"
            f"  Corners casa: {nd(hd['home']['corners'])} | Disparos: {nd(hd['home']['shots'])} | Tarjetas: {nd(hd['home']['cards'])}\n"
            f"  De visitante: {away_results}\n"
            f"  Forma fuera: {hd['away']['form']} | Goles: {nd(hd['away']['gf'])} marc / {nd(hd['away']['ga'])} enc\n"
            f"  Corners fuera: {nd(hd['away']['corners'])} | Disparos: {nd(hd['away']['shots'])} | Tarjetas: {nd(hd['away']['cards'])}"
        )
    else:
        blocks.append(f"🔵 {home.upper()} — No encontrado en API-Football")

    if data["away_data"]:
        ad = data["away_data"]
        an = data["away_team"]["team"]["name"]
        home_results = " · ".join([fmt_result(f, data["away_team"]["team"]["id"]) for f in ad["home"]["fixes"]])
        away_results = " · ".join([fmt_result(f, data["away_team"]["team"]["id"]) for f in ad["away"]["fixes"]])
        blocks.append(
            f"\n🔴 {an.upper()}\n"
            f"  En casa: {home_results}\n"
            f"  Forma casa: {ad['home']['form']} | Goles: {nd(ad['home']['gf'])} marc / {nd(ad['home']['ga'])} enc\n"
            f"  Corners casa: {nd(ad['home']['corners'])} | Disparos: {nd(ad['home']['shots'])} | Tarjetas: {nd(ad['home']['cards'])}\n"
            f"  De visitante: {away_results}\n"
            f"  Forma fuera: {ad['away']['form']} | Goles: {nd(ad['away']['gf'])} marc / {nd(ad['away']['ga'])} enc\n"
            f"  Corners fuera: {nd(ad['away']['corners'])} | Disparos: {nd(ad['away']['shots'])} | Tarjetas: {nd(ad['away']['cards'])}"
        )
    else:
        blocks.append(f"\n🔴 {away.upper()} — No encontrado en API-Football")

    if data["h2h"]:
        h2h_lines = []
        for fix in data["h2h"][:5]:
            d = fix["fixture"]["date"][:10]
            gh = fix["goals"]["home"]
            ga = fix["goals"]["away"]
            hn2 = fix["teams"]["home"]["name"][:10]
            an2 = fix["teams"]["away"]["name"][:10]
            h2h_lines.append(f"  {d} {hn2} {gh}-{ga} {an2}")
        blocks.append("\n⚔️ H2H:\n" + "\n".join(h2h_lines))

    data_str = "\n".join(blocks)

    web_instruction = "" if data["api_ok"] else f"""
API-Football no tiene datos de estos equipos. Usa web_search ANTES de responder:
1. "sofascore {home} resultados estadísticas 2026"
2. "sofascore {away} resultados estadísticas 2026"
3. "{home} {away} head to head sofascore"
4. "{home} corners tarjetas por partido estadísticas"
5. "{away} corners tarjetas por partido estadísticas"
"""

    cond_short = "\n".join(
        f'• {c["label"]} (peso {c["weight"]})'
        for c in conditions
    )

    return f"""Analista deportivo experto. Informe claro y visual para Telegram del partido *{home}* vs *{away}*.

{web_instruction}
DATOS:
{data_str}

Si hay datos "s/d", búscalos en Sofascore con web_search.

FORMATO (español, sin tecnicismos, máx 3500 caracteres):

⚽ *{home.upper()} vs {away.upper()}*
_[competición]_

━━━━━━━━━━━━━━━━
🔵 *{home} en casa*
[últimos 5: ✅Ganó X-X vs Rival · ❌Perdió · 🟡Empató]
• Gana el X% en casa | Marca X goles, encaja X de media
• Córners: X/partido | Disparos: X/partido | Tarjetas: X/partido

🔵 *{home} fuera*
• X victorias, X empates, X derrotas en últimos 5 fuera
• Marca X y encaja X de media fuera

━━━━━━━━━━━━━━━━
🔴 *{away} fuera*
[últimos 5 fuera: ✅/❌/🟡]
• Gana el X% fuera | Marca X, encaja X de media fuera
• Córners: X/partido | Disparos: X/partido | Tarjetas: X/partido

🔴 *{away} en casa*
• X victorias, X empates, X derrotas en últimos 5 en casa

━━━━━━━━━━━━━━━━
⚔️ *Historial directo*
[fecha] Equipo X-X Equipo
📌 [equipo] domina con X victorias. Media X goles/partido.

━━━━━━━━━━━━━━━━
✅ *Condiciones* (✅ se cumple · ❌ no · ⚠️ sin datos)
{cond_short}
[Para cada una: ✅/❌/⚠️ Nombre — una frase explicando por qué]

━━━━━━━━━━━━━━━━
📊 *Puntuación: X/{max_pts} pts ([X]%)*
[████████░░] 🟢 FAVORABLE / 🟡 DUDOSO / 🔴 NO RECOMENDABLE

🔮 *Conclusión:* [2 frases: qué mercados avalan los datos]
📡 _{"API-Football" if data["api_ok"] else "Sofascore"} · {now}_
"""


async def analyze_match(home: str, away: str, conditions: list[dict] | None = None) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    data = await build_real_data(home, away)
    logger.info(f"API-Football ok={data['api_ok']} for {home} vs {away}")

    prompt = build_prompt(home, away, conditions, data)

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
            "Usas los datos reales proporcionados fielmente. "
            "Para datos faltantes (s/d) usas web_search en Sofascore/Flashscore. "
            "NUNCA inventas estadísticas. El formato debe ser limpio y visual para Telegram. "
            "Sin tablas markdown. Respuestas concisas y directas."
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            data_r = r.json()
            text_parts = [
                block["text"]
                for block in data_r.get("content", [])
                if block.get("type") == "text"
            ]
            return "\n".join(text_parts) if text_parts else "❌ No se pudo generar el análisis."
    except httpx.HTTPStatusError as e:
        logger.error(f"Anthropic API error: {e.response.text}")
        return "❌ Error al generar el análisis. Inténtalo de nuevo en unos segundos."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "❌ Error inesperado. Revisa los logs del servidor."
