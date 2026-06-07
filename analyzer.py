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
    {"id": "btts",         "label": "Ambos equipos marcan (BTTS)",                 "weight": 8},
    {"id": "over25",       "label": "Más de 2.5 goles en el partido",               "weight": 7},
    {"id": "home_form",    "label": "El local tiene mejor forma reciente",          "weight": 6},
    {"id": "away_goals",   "label": "Visitante promedia más de 1.5 goles/partido",  "weight": 5},
    {"id": "clean_sheet",  "label": "Al menos un equipo con portería a 0 últimos 3","weight": 4},
    {"id": "home_unbeaten","label": "Local invicto en sus últimos 5",               "weight": 6},
    {"id": "h2h_goals",    "label": "H2H: ambos equipos marcan",                   "weight": 5},
    {"id": "over15",       "label": "Más de 1.5 goles en el partido",               "weight": 5},
    {"id": "home_goals",   "label": "Local promedia más de 1.5 goles en casa",      "weight": 5},
    {"id": "away_concede", "label": "Visitante encaja en todos sus partidos fuera", "weight": 4},
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

async def get_h2h(id1: int, id2: int) -> list:
    try:
        data = await apif("fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": 6})
        return data.get("response", [])
    except Exception as e:
        return []

def avg(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v)/len(v), 1) if v else None

def get_result(fix: dict, team_id: int) -> str:
    gh = fix["goals"]["home"] or 0
    ga = fix["goals"]["away"] or 0
    is_home = fix["teams"]["home"]["id"] == team_id
    gf = gh if is_home else ga
    gc = ga if is_home else gh
    return "W" if gf > gc else "D" if gf == gc else "L"

def fmt_result(fix: dict, team_id: int) -> str:
    if fix["goals"]["home"] is None:
        return "?"
    r = get_result(fix, team_id)
    is_home = fix["teams"]["home"]["id"] == team_id
    gh = fix["goals"]["home"]
    ga = fix["goals"]["away"]
    opp = fix["teams"]["away"]["name"] if is_home else fix["teams"]["home"]["name"]
    emoji = "✅" if r == "W" else "🟡" if r == "D" else "❌"
    return f"{emoji}{gh}-{ga}{opp[:6]}"

async def team_data(team_id: int) -> dict:
    fixes = await get_fixtures(team_id, 12)
    home_fixes = [f for f in fixes if f["teams"]["home"]["id"] == team_id and f["goals"]["home"] is not None]
    away_fixes = [f for f in fixes if f["teams"]["away"]["id"] == team_id and f["goals"]["home"] is not None]

    def calc(fix_list):
        gf_l, ga_l = [], []
        results = []
        for fix in fix_list[:6]:
            is_h = fix["teams"]["home"]["id"] == team_id
            gh = fix["goals"]["home"] or 0
            ga = fix["goals"]["away"] or 0
            gf_l.append(gh if is_h else ga)
            ga_l.append(ga if is_h else gh)
            results.append(get_result(fix, team_id))
        return {
            "form": "".join(results[:5]),
            "gf": avg(gf_l), "ga": avg(ga_l),
            "fixes": fix_list[:5],
            "gf_list": gf_l, "ga_list": ga_l,
        }

    return {
        "home": calc(home_fixes),
        "away": calc(away_fixes),
    }

async def build_real_data(home_name: str, away_name: str) -> dict:
    ht = await find_team(home_name)
    at = await find_team(away_name)
    api_ok = ht is not None or at is not None

    result = {"home_team": ht, "away_team": at, "api_ok": api_ok,
              "home_data": None, "away_data": None, "h2h": []}

    if ht:
        result["home_data"] = await team_data(ht["team"]["id"])
    if at:
        result["away_data"] = await team_data(at["team"]["id"])
    if ht and at:
        result["h2h"] = await get_h2h(ht["team"]["id"], at["team"]["id"])

    return result

def nd(val):
    return str(val) if val is not None else "s/d"

def build_prompt(home: str, away: str, conditions: list[dict], data: dict) -> str:
    now = datetime.now().strftime("%d/%m/%Y")
    max_pts = sum(c["weight"] for c in conditions)
    hd = data.get("home_data") or {}
    ad = data.get("away_data") or {}

    blocks = [f"=== DATOS REALES ({now}) ===\n"]

    if data.get("home_data"):
        hn = data["home_team"]["team"]["name"]
        home_res = " ".join([fmt_result(f, data["home_team"]["team"]["id"]) for f in hd["home"]["fixes"]])
        away_res = " ".join([fmt_result(f, data["home_team"]["team"]["id"]) for f in hd["away"]["fixes"]])
        blocks.append(
            f"🔵 {hn.upper()}\n"
            f"  Casa: {home_res} | Media goles: {nd(hd['home']['gf'])} marc / {nd(hd['home']['ga'])} enc\n"
            f"  Fuera: {away_res} | Media goles: {nd(hd['away']['gf'])} marc / {nd(hd['away']['ga'])} enc"
        )
    else:
        blocks.append(f"🔵 {home.upper()} — No encontrado en API-Football")

    if data.get("away_data"):
        an = data["away_team"]["team"]["name"]
        home_res = " ".join([fmt_result(f, data["away_team"]["team"]["id"]) for f in ad["home"]["fixes"]])
        away_res = " ".join([fmt_result(f, data["away_team"]["team"]["id"]) for f in ad["away"]["fixes"]])
        blocks.append(
            f"\n🔴 {an.upper()}\n"
            f"  Fuera: {away_res} | Media goles: {nd(ad['away']['gf'])} marc / {nd(ad['away']['ga'])} enc\n"
            f"  Casa: {home_res} | Media goles: {nd(ad['home']['gf'])} marc / {nd(ad['home']['ga'])} enc"
        )
    else:
        blocks.append(f"\n🔴 {away.upper()} — No encontrado en API-Football")

    if data.get("h2h"):
        h2h_lines = []
        for fix in data["h2h"][:3]:
            d = fix["fixture"]["date"][:10]
            gh = fix["goals"]["home"]
            ga = fix["goals"]["away"]
            hn2 = fix["teams"]["home"]["name"][:8]
            an2 = fix["teams"]["away"]["name"][:8]
            h2h_lines.append(f"{d} {hn2} {gh}-{ga} {an2}")
        blocks.append("\n⚔️ H2H: " + " | ".join(h2h_lines))

    data_str = "\n".join(blocks)

    web_instruction = "" if data["api_ok"] else f"""
API-Football no tiene datos. Usa web_search:
1. "sofascore {home} resultados 2026"
2. "sofascore {away} resultados 2026"
3. "{home} {away} head to head"
"""

    cond_list = "\n".join(f'• {c["label"]} (peso {c["weight"]})' for c in conditions)

    return f"""Analista deportivo. Análisis BREVE para Telegram. Máximo 1800 caracteres.

REGLA CRÍTICA: USA SOLO los datos proporcionados partido a partido.
NUNCA inventes ni calcules porcentajes o promedios que no estén en los datos.
Si no tienes un dato concreto, no lo menciones.

{web_instruction}
DATOS:
{data_str}

CONDICIONES A EVALUAR:
{cond_list}

FORMATO EXACTO:

⚽ *{home.upper()} vs {away.upper()}*
_[competición] · {now}_

🔵 *{home}* · [✅❌🟡 x5 resultados casa en una línea]
Goles casa: X marc / X enc de media

🔴 *{away}* · [✅❌🟡 x5 resultados fuera en una línea]
Goles fuera: X marc / X enc de media

⚔️ *H2H* · [últimos 3] · media goles: X

━━━━━━━━━━━━━━━━
✅ *Condiciones*
[cada una en UNA línea: ✅/❌ Nombre — motivo breve basado SOLO en datos reales]

📊 *X/{max_pts} pts · X%*
🟢 FAVORABLE / 🟡 DUDOSO / 🔴 NO RECOMENDABLE

🔮 [1 frase conclusión]
📡 _{"API-Football" if data["api_ok"] else "Sofascore"} · {now}_"""


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
        "max_tokens": 3000,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en fútbol. Respondes siempre en español. "
            "Usas SOLO los datos reales proporcionados. "
            "NUNCA inventes estadísticas, porcentajes ni promedios. "
            "Si no tienes un dato, no lo menciones. "
            "Formato Markdown Telegram. Respuestas concisas."
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
