import os
import json
import re
import math
import httpx
import psycopg2
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
APIFOOTBALL_KEY    = os.getenv("APIFOOTBALL_KEY", "")
HIGHLIGHTLY_KEY    = os.getenv("HIGHLIGHTLY_KEY", "")
DATABASE_URL       = os.getenv("DATABASE_URL", "")
ANTHROPIC_URL      = "https://api.anthropic.com/v1/messages"
APIFOOTBALL_URL    = "https://v3.football.api-sports.io"
HIGHLIGHTLY_URL    = "https://api.highlightly.net/v1"

DEFAULT_CONDITIONS = [
    {"id": "btts",         "label": "Ambos equipos marcan (BTTS)",                  "weight": 8},
    {"id": "over25",       "label": "Mas de 2.5 goles en el partido",                "weight": 7},
    {"id": "home_form",    "label": "El local tiene mejor forma reciente",           "weight": 6},
    {"id": "away_goals",   "label": "Visitante promedia mas de 1.5 goles/partido",   "weight": 5},
    {"id": "clean_sheet",  "label": "Al menos un equipo con porteria a 0 ultimos 3", "weight": 4},
    {"id": "home_unbeaten","label": "Local invicto en sus ultimos 5",                "weight": 6},
    {"id": "h2h_goals",    "label": "H2H: ambos equipos marcan",                    "weight": 5},
    {"id": "over15",       "label": "Mas de 1.5 goles en el partido",                "weight": 5},
    {"id": "home_goals",   "label": "Local promedia mas de 1.5 goles en casa",       "weight": 5},
    {"id": "away_concede", "label": "Visitante encaja goles con frecuencia fuera",  "weight": 4},
    {"id": "corners_over85", "label": "Mas de 8.5 corners en el partido",           "weight": 6},
    {"id": "cards_over35",   "label": "Mas de 3.5 tarjetas en el partido",          "weight": 6},
]

def avg(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v)/len(v), 1) if v else None

# Base de datos (Supabase)

# Prefijos/sufijos de club muy comunes que suelen variar entre fuentes de
# datos distintas (p.ej. "IBV Vestmannaeyjar" en una fuente vs solo
# "Vestmannaeyjar" en otra). Se usan para generar variantes de búsqueda.
_CLUB_TOKENS_COMUNES = {
    "FC", "CF", "SC", "AC", "AS", "CD", "SD", "UD", "RC", "CA", "CE", "EC",
    "AFC", "SK", "BK", "IF", "IK", "IBV", "KS", "FK", "US", "RS",
}


def _name_variants(name: str) -> set:
    """Genera variantes de un nombre de equipo (con y sin abreviaturas de
    club habituales) para poder encontrarlo aunque distintas fuentes de
    datos lo escriban de forma diferente."""
    name = (name or "").strip()
    if not name:
        return set()
    variants = {name}
    words = name.split()
    if len(words) > 1:
        # Quita un posible prefijo/sufijo abreviado en mayúsculas (ej. "IBV Vestmannaeyjar")
        if words[0].isupper() and len(words[0]) <= 4:
            variants.add(" ".join(words[1:]))
        if words[-1].isupper() and len(words[-1]) <= 4:
            variants.add(" ".join(words[:-1]))
        # Quita tokens de club muy comunes (FC, CD, SK...) estén donde estén
        filtered = [w for w in words if w.upper() not in _CLUB_TOKENS_COMUNES]
        if filtered and filtered != words:
            variants.add(" ".join(filtered))
    return {v for v in variants if v}


def _names_match(a: str, b: str) -> bool:
    """Compara dos nombres de equipo de forma tolerante: prueba varias
    variantes de 'a' contra 'b' en ambas direcciones, en vez de exigir
    que uno contenga literalmente al otro completo."""
    b_low = (b or "").lower().strip()
    if not b_low:
        return False
    for v in _name_variants(a):
        v_low = v.lower()
        if v_low in b_low or b_low in v_low:
            return True
    return False


def db_get_team_rows(name: str, limit: int = 12) -> list:
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        variants = _name_variants(name)
        conditions = []
        params: list = []
        for v in variants:
            conditions.append("equipo_local ILIKE %s")
            params.append(f"%{v}%")
            conditions.append("equipo_visitante ILIKE %s")
            params.append(f"%{v}%")
        where_clause = " OR ".join(conditions)
        params.append(limit)
        cur.execute(f"""
            SELECT DISTINCT equipo_local, equipo_visitante, goles_local, goles_visitante,
                   corners_local, corners_visitante,
                   tarjetas_amarillas_local, tarjetas_amarillas_visitante,
                   tarjetas_rojas_local, tarjetas_rojas_visitante,
                   tiros_puerta_local, tiros_puerta_visitante,
                   fecha
            FROM partidos
            WHERE {where_clause}
            ORDER BY fecha DESC
            LIMIT %s
        """, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
        conn.close()
        result = [dict(zip(cols, r)) for r in rows]
        print(f"[DEBUG] db_get_team_rows({name}): {len(result)} filas encontradas (variantes: {variants})")
        return result
    except Exception as e:
        print(f"[DEBUG] db_get_team_rows({name}) FALLO: {type(e).__name__}: {e}")
        return []

def db_team_data(name: str) -> dict | None:
    rows = db_get_team_rows(name, 12)
    if not rows:
        return None

    home_rows = [r for r in rows if _names_match(name, r["equipo_local"])]
    away_rows = [r for r in rows if _names_match(name, r["equipo_visitante"])]

    matched_name = name
    if home_rows:
        matched_name = home_rows[0]["equipo_local"]
    elif away_rows:
        matched_name = away_rows[0]["equipo_visitante"]

    def calc(row_list, is_home):
        gf_l, ga_l, corners_l, shots_l, cards_l = [], [], [], [], []
        results_fmt = []
        for row in row_list[:6]:
            if is_home:
                gf, gc = row["goles_local"], row["goles_visitante"]
                corners = row["corners_local"]
                shots = row["tiros_puerta_local"]
                ca = row["tarjetas_amarillas_local"] or 0
                cr = row["tarjetas_rojas_local"] or 0
                opp = row["equipo_visitante"]
            else:
                gf, gc = row["goles_visitante"], row["goles_local"]
                corners = row["corners_visitante"]
                shots = row["tiros_puerta_visitante"]
                ca = row["tarjetas_amarillas_visitante"] or 0
                cr = row["tarjetas_rojas_visitante"] or 0
                opp = row["equipo_local"]
            if gf is None or gc is None:
                continue
            gf_l.append(gf)
            ga_l.append(gc)
            if corners is not None:
                corners_l.append(corners)
            if shots is not None:
                shots_l.append(shots)
            cards_l.append(ca + cr)
            r = "W" if gf > gc else "D" if gf == gc else "L"
            emoji = "OK" if r == "W" else "EQ" if r == "D" else "NO"
            results_fmt.append(f"{emoji}{gf}-{gc} {(opp or '?')[:7]}")
        return {
            "results": results_fmt[:5],
            "gf": avg(gf_l), "ga": avg(ga_l),
            "corners": avg(corners_l),
            "shots": avg(shots_l),
            "cards": avg(cards_l),
        }

    return {
        "home": calc(home_rows, True),
        "away": calc(away_rows, False),
        "source": "Base de datos propia",
        "matched_name": matched_name,
    }

# API-Football

async def apif(endpoint: str, params: dict) -> dict:
    headers = {
        "x-apisports-key": APIFOOTBALL_KEY,
        "x-rapidapi-host": "v3.football.api-sports.io",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{APIFOOTBALL_URL}/{endpoint}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()

async def apif_find_team(name: str) -> dict | None:
    try:
        data = await apif("teams", {"search": name})
        results = data.get("response", [])
        if not results:
            print(f"[DEBUG] apif_find_team({name}): 0 resultados")
            return None
        for r in results:
            if name.lower() in r["team"]["name"].lower():
                return r
        return results[0]
    except Exception as e:
        print(f"[DEBUG] apif_find_team({name}) FALLO: {type(e).__name__}: {e}")
        return None

async def apif_get_fixtures(team_id: int, last: int = 12) -> list:
    try:
        data = await apif("fixtures", {"team": team_id, "last": last})
        return data.get("response", [])
    except Exception as e:
        print(f"[DEBUG] apif_get_fixtures FALLO: {type(e).__name__}: {e}")
        return []

async def apif_get_h2h(id1: int, id2: int) -> list:
    try:
        data = await apif("fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": 6})
        return data.get("response", [])
    except Exception as e:
        return []

async def apif_get_fixture_stats(fixture_id: int) -> list:
    try:
        data = await apif("fixtures/statistics", {"fixture": fixture_id})
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

# Highlightly

async def hl(endpoint: str, params: dict) -> dict:
    headers = {"x-api-key": HIGHLIGHTLY_KEY}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{HIGHLIGHTLY_URL}/{endpoint}", headers=headers, params=params)
        r.raise_for_status()
        return r.json()

async def hl_find_team(name: str) -> dict | None:
    try:
        data = await hl("teams", {"search": name})
        results = data.get("data", data.get("teams", data.get("response", [])))
        if not results:
            return None
        for r in results:
            tname = r.get("name", r.get("team", {}).get("name", ""))
            if name.lower() in tname.lower():
                return r
        return results[0]
    except Exception as e:
        print(f"[DEBUG] hl_find_team({name}) FALLO: {type(e).__name__}: {e}")
        return None

async def hl_get_fixtures(team_id, last: int = 10) -> list:
    try:
        data = await hl("fixtures", {"team": team_id, "last": last})
        return data.get("data", data.get("fixtures", data.get("response", [])))
    except Exception as e:
        print(f"[DEBUG] hl_get_fixtures FALLO: {type(e).__name__}: {e}")
        return []

async def hl_get_h2h(id1, id2, last: int = 5) -> list:
    try:
        data = await hl("fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": last})
        return data.get("data", data.get("fixtures", data.get("response", [])))
    except Exception as e:
        return []

def nd(val):
    return str(val) if val is not None else None

def get_result_apif(fix: dict, team_id: int) -> str:
    gh = fix["goals"]["home"] or 0
    ga = fix["goals"]["away"] or 0
    is_home = fix["teams"]["home"]["id"] == team_id
    gf = gh if is_home else ga
    gc = ga if is_home else gh
    return "W" if gf > gc else "D" if gf == gc else "L"

def fmt_result_apif(fix: dict, team_id: int) -> str:
    if fix["goals"]["home"] is None:
        return None
    r = get_result_apif(fix, team_id)
    is_home = fix["teams"]["home"]["id"] == team_id
    gh = fix["goals"]["home"]
    ga = fix["goals"]["away"]
    opp = fix["teams"]["away"]["name"] if is_home else fix["teams"]["home"]["name"]
    emoji = "OK" if r == "W" else "EQ" if r == "D" else "NO"
    return f"{emoji}{gh}-{ga} {opp[:7]}"

async def apif_team_data(team_id: int) -> dict:
    fixes = await apif_get_fixtures(team_id, 12)
    home_fixes = [f for f in fixes if f["teams"]["home"]["id"] == team_id and f["goals"]["home"] is not None]
    away_fixes = [f for f in fixes if f["teams"]["away"]["id"] == team_id and f["goals"]["home"] is not None]

    async def calc(fix_list, loc):
        gf_l, ga_l = [], []
        corners_l, shots_l, cards_l = [], [], []
        results_fmt = []
        for fix in fix_list[:6]:
            is_h = fix["teams"]["home"]["id"] == team_id
            gh = fix["goals"]["home"] or 0
            ga = fix["goals"]["away"] or 0
            gf_l.append(gh if is_h else ga)
            ga_l.append(ga if is_h else gh)
            fmt = fmt_result_apif(fix, team_id)
            if fmt:
                results_fmt.append(fmt)
            stats = await apif_get_fixture_stats(fix["fixture"]["id"])
            if stats:
                h_id = fix["teams"]["home"]["id"]
                a_id = fix["teams"]["away"]["id"]
                c = sv(stats, h_id if is_h else a_id, "Corner Kicks")
                s = sv(stats, h_id if is_h else a_id, "Shots on Goal")
                yh = sv(stats, h_id, "Yellow Cards") or 0
                ya = sv(stats, a_id, "Yellow Cards") or 0
                rh = sv(stats, h_id, "Red Cards") or 0
                ra = sv(stats, a_id, "Red Cards") or 0
                if c is not None: corners_l.append(c)
                if s is not None: shots_l.append(s)
                cards_l.append(yh + ya + rh + ra)
        return {
            "results": results_fmt[:5],
            "gf": avg(gf_l), "ga": avg(ga_l),
            "corners": avg(corners_l),
            "shots": avg(shots_l),
            "cards": avg(cards_l),
        }

    return {
        "home": await calc(home_fixes, "home"),
        "away": await calc(away_fixes, "away"),
        "source": "API-Football",
    }

async def hl_team_data(team_id) -> dict:
    fixes = await hl_get_fixtures(team_id, 10)
    home_fixes, away_fixes = [], []

    for f in fixes:
        home_id = f.get("homeTeam", {}).get("id") or f.get("home", {}).get("id") or f.get("teams", {}).get("home", {}).get("id")
        away_id = f.get("awayTeam", {}).get("id") or f.get("away", {}).get("id") or f.get("teams", {}).get("away", {}).get("id")
        if str(home_id) == str(team_id):
            home_fixes.append(f)
        elif str(away_id) == str(team_id):
            away_fixes.append(f)

    def extract_goals(fix, is_home):
        gh = fix.get("homeScore") or fix.get("score", {}).get("home") or fix.get("goals", {}).get("home")
        ga = fix.get("awayScore") or fix.get("score", {}).get("away") or fix.get("goals", {}).get("away")
        if gh is None or ga is None:
            return None, None
        return (int(gh), int(ga))

    def calc(fix_list, is_home_list):
        gf_l, ga_l = [], []
        results_fmt = []
        for fix, is_home in zip(fix_list[:5], is_home_list[:5]):
            gh, ga = extract_goals(fix, is_home)
            if gh is None:
                continue
            gf = gh if is_home else ga
            gc = ga if is_home else gh
            gf_l.append(gf)
            ga_l.append(gc)
            r = "W" if gf > gc else "D" if gf == gc else "L"
            emoji = "OK" if r == "W" else "EQ" if r == "D" else "NO"
            home_name = fix.get("homeTeam", {}).get("name", fix.get("home", {}).get("name", "?"))[:7]
            away_name = fix.get("awayTeam", {}).get("name", fix.get("away", {}).get("name", "?"))[:7]
            opp = away_name if is_home else home_name
            results_fmt.append(f"{emoji}{gh}-{ga} {opp}")
        return {
            "results": results_fmt,
            "gf": avg(gf_l), "ga": avg(ga_l),
            "corners": None, "shots": None, "cards": None,
        }

    return {
        "home": calc(home_fixes, [True]*len(home_fixes)),
        "away": calc(away_fixes, [False]*len(away_fixes)),
        "source": "Highlightly",
    }

# Constructor principal de datos: primero base de datos propia, luego APIs en vivo

async def build_real_data(home_name: str, away_name: str) -> dict:
    sources = []

    home_db = db_team_data(home_name)
    away_db = db_team_data(away_name)

    home_db_ok = bool(home_db and (home_db["home"]["results"] or home_db["away"]["results"]))
    away_db_ok = bool(away_db and (away_db["home"]["results"] or away_db["away"]["results"]))

    home_data = home_db if home_db_ok else None
    away_data = away_db if away_db_ok else None
    home_team_info = {"team": {"name": home_db["matched_name"]}} if home_db_ok else None
    away_team_info = {"team": {"name": away_db["matched_name"]}} if away_db_ok else None

    if home_db_ok:
        sources.append("Base de datos propia")
    if away_db_ok:
        sources.append("Base de datos propia")

    ht_apif = None
    at_apif = None
    h2h = []

    if not home_db_ok:
        ht_apif = await apif_find_team(home_name)
        if ht_apif:
            home_data = await apif_team_data(ht_apif["team"]["id"])
            home_team_info = ht_apif
            sources.append("API-Football")

    if not away_db_ok:
        at_apif = await apif_find_team(away_name)
        if at_apif:
            away_data = await apif_team_data(at_apif["team"]["id"])
            away_team_info = at_apif
            sources.append("API-Football")

    if ht_apif and at_apif:
        h2h = await apif_get_h2h(ht_apif["team"]["id"], at_apif["team"]["id"])

    home_needs_hl = (not home_db_ok) and (not home_data or (home_data["home"]["corners"] is None and home_data["away"]["corners"] is None))
    away_needs_hl = (not away_db_ok) and (not away_data or (away_data["home"]["corners"] is None and away_data["away"]["corners"] is None))

    if home_needs_hl:
        ht_hl = await hl_find_team(home_name)
        if ht_hl:
            hl_data = await hl_team_data(ht_hl.get("id") or ht_hl.get("team", {}).get("id"))
            if home_data:
                for loc in ["home", "away"]:
                    if home_data[loc]["corners"] is None:
                        home_data[loc]["corners"] = hl_data[loc].get("corners")
                    if home_data[loc]["shots"] is None:
                        home_data[loc]["shots"] = hl_data[loc].get("shots")
                    if home_data[loc]["cards"] is None:
                        home_data[loc]["cards"] = hl_data[loc].get("cards")
                home_data["source"] = "API-Football + Highlightly"
            else:
                home_data = hl_data
                home_team_info = ht_hl
            sources.append("Highlightly")

    if away_needs_hl:
        at_hl = await hl_find_team(away_name)
        if at_hl:
            hl_data = await hl_team_data(at_hl.get("id") or at_hl.get("team", {}).get("id"))
            if away_data:
                for loc in ["home", "away"]:
                    if away_data[loc]["corners"] is None:
                        away_data[loc]["corners"] = hl_data[loc].get("corners")
                    if away_data[loc]["shots"] is None:
                        away_data[loc]["shots"] = hl_data[loc].get("shots")
                    if away_data[loc]["cards"] is None:
                        away_data[loc]["cards"] = hl_data[loc].get("cards")
                away_data["source"] = "API-Football + Highlightly"
            else:
                away_data = hl_data
                away_team_info = at_hl
            sources.append("Highlightly")

    api_ok = home_data is not None or away_data is not None
    source_str = " + ".join(dict.fromkeys(sources)) if sources else "Busqueda web"

    both_found = home_data is not None and away_data is not None
    has_results = (
        bool(home_data and (home_data.get("home", {}).get("results") or home_data.get("away", {}).get("results"))) and
        bool(away_data and (away_data.get("home", {}).get("results") or away_data.get("away", {}).get("results")))
    )

    if both_found and has_results and sources:
        confidence = "high"
    elif api_ok and has_results:
        confidence = "medium"
    else:
        confidence = "low"

    result = {
        "home_team": home_team_info,
        "away_team": away_team_info,
        "home_data": home_data,
        "away_data": away_data,
        "h2h": h2h,
        "api_ok": api_ok,
        "source": source_str,
        "confidence": confidence,
    }
    print(f"[DEBUG] build_real_data({home_name}, {away_name}) -> api_ok={api_ok} source={source_str} confidence={confidence} home_db={home_db_ok} away_db={away_db_ok} home_apif={ht_apif is not None} away_apif={at_apif is not None}")
    return result

# ─────────────────────────────────────────────────────────────────────────
# Motor deterministico de condiciones
# ─────────────────────────────────────────────────────────────────────────
# Antes esto se lo pediamos al LLM: le pasabamos el volcado de datos en texto
# y tenia que leerlo, decidir si/no para cada una de las 12 condiciones,
# sumar los pesos y calcular el %. Eso gastaba tokens (de entrada y de
# salida) en algo que es aritmetica pura sobre datos que YA estan calculados
# aqui abajo en Python, y dejaba una decision binaria en manos de un modelo
# de lenguaje en vez de una regla fija y repetible. Ahora se calcula en esta
# funcion; el LLM (ver _render_report) solo redacta el resultado, nunca lo
# decide.
#
# Para btts / over25 / home_goals / away_goals / corners_over85 / cards_over35
# se usa el modelo estadistico Poisson + calibracion Platt ya validado (ver
# poisson_calibrated_probs mas abajo) cuando hay datos suficientes en la BD.
# El resto de condiciones (y esas mismas cuando el modelo no tiene datos
# suficientes) usan un umbral simple sobre las medias ya calculadas en
# build_real_data -- no son un modelo validado con backtesting, son la misma
# clase de heuristica que antes hacia el LLM "a ojo", solo que ahora es
# reproducible y testeable.


def _parse_result_letter(res: str) -> str | None:
    """'OK1-0 Barce' -> 'W' ; 'EQ1-1 Betis' -> 'D' ; 'NO0-2 Betis' -> 'L'."""
    if not res:
        return None
    if res.startswith("OK"):
        return "W"
    if res.startswith("EQ"):
        return "D"
    if res.startswith("NO"):
        return "L"
    return None


def _parse_result_goals(res: str) -> tuple[int, int] | None:
    """'OK2-1 Betis' -> (2, 1) : (goles a favor, goles en contra)."""
    m = re.match(r"^(?:OK|EQ|NO)(\d+)-(\d+)", res or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _form_points(results: list[str]) -> int:
    pts = {"W": 3, "D": 1, "L": 0}
    return sum(pts.get(_parse_result_letter(r), 0) for r in results)


def _clean_sheet_in_last3(results: list[str]) -> bool:
    for res in (results or [])[:3]:
        parsed = _parse_result_goals(res)
        if parsed and parsed[1] == 0:
            return True
    return False


def _h2h_both_score_ratio(h2h: list[dict]) -> tuple[int, int] | None:
    total = 0
    both = 0
    for fix in (h2h or [])[:5]:
        gh = fix.get("goals", {}).get("home")
        ga = fix.get("goals", {}).get("away")
        if gh is None or ga is None:
            continue
        total += 1
        if gh > 0 and ga > 0:
            both += 1
    return (both, total) if total else None


def evaluate_conditions(home: str, away: str, conditions: list[dict], data: dict) -> dict:
    """Decide si/no para cada condicion y la puntuacion total sin llamar al
    LLM. Devuelve {"items", "score", "max_pts", "pct", "verdict", "stat_probs"}.
    Cada item: {"id","label","weight","status" (bool), "reason"}. Si no hay
    datos suficientes para una condicion se marca status=False con motivo
    "Datos insuficientes" -- igual que haria un analista sin evidencia para
    respaldarla, nunca se inventa un "si"."""
    hd = data.get("home_data") or {}
    ad = data.get("away_data") or {}
    h_home = hd.get("home", {}) or {}
    a_away = ad.get("away", {}) or {}
    home_results = h_home.get("results") or []
    away_results = a_away.get("results") or []

    stat_probs = poisson_calibrated_probs(home, away)
    results: dict[str, tuple[bool, str]] = {}

    def from_stat_or(cond_id, fallback):
        prob = stat_probs.get(cond_id)
        if prob is not None:
            return prob >= 50, f"Modelo estadistico calibrado: {prob:.0f}% de probabilidad"
        return fallback

    gf_h, gf_a = h_home.get("gf"), a_away.get("gf")

    if gf_h is not None and gf_a is not None:
        fallback_btts = (gf_h >= 1.0 and gf_a >= 1.0, f"Medias: {gf_h} / {gf_a} goles marcados")
        fallback_over25 = (gf_h + gf_a > 2.5, f"Media combinada estimada: {round(gf_h + gf_a, 1)}")
        fallback_over15 = (gf_h + gf_a > 1.5, f"Media combinada estimada: {round(gf_h + gf_a, 1)}")
    else:
        fallback_btts = fallback_over25 = fallback_over15 = (False, "Datos insuficientes")

    results["btts"] = from_stat_or("btts", fallback_btts)
    results["over25"] = from_stat_or("over25", fallback_over25)
    results["over15"] = fallback_over15  # sin modelo Platt propio validado

    if gf_h is not None:
        results["home_goals"] = from_stat_or("home_goals", (gf_h > 1.5, f"Media local en casa: {gf_h}"))
    else:
        results["home_goals"] = from_stat_or("home_goals", (False, "Datos insuficientes"))

    if gf_a is not None:
        results["away_goals"] = from_stat_or("away_goals", (gf_a > 1.5, f"Media visitante fuera: {gf_a}"))
    else:
        results["away_goals"] = from_stat_or("away_goals", (False, "Datos insuficientes"))

    c_h, c_a = h_home.get("corners"), a_away.get("corners")
    if c_h is not None and c_a is not None:
        results["corners_over85"] = from_stat_or("corners_over85", (c_h + c_a > 8.5, f"Media combinada de corners: {round(c_h + c_a, 1)}"))
    else:
        results["corners_over85"] = from_stat_or("corners_over85", (False, "Datos insuficientes"))

    j_h, j_a = h_home.get("cards"), a_away.get("cards")
    if j_h is not None and j_a is not None:
        results["cards_over35"] = from_stat_or("cards_over35", (j_h + j_a > 3.5, f"Media combinada de tarjetas: {round(j_h + j_a, 1)}"))
    else:
        results["cards_over35"] = from_stat_or("cards_over35", (False, "Datos insuficientes"))

    ga_a = a_away.get("ga")
    if ga_a is not None:
        results["away_concede"] = (ga_a >= 1.3, f"Media encajada fuera: {ga_a}")
    else:
        results["away_concede"] = (False, "Datos insuficientes")

    if home_results:
        unbeaten = all(_parse_result_letter(r) != "L" for r in home_results)
        results["home_unbeaten"] = (unbeaten, f"{'Sin derrotas' if unbeaten else 'Con derrotas'} en sus ultimos {len(home_results)} como local")
    else:
        results["home_unbeaten"] = (False, "Datos insuficientes")

    if home_results or away_results:
        cs = _clean_sheet_in_last3(home_results) or _clean_sheet_in_last3(away_results)
        results["clean_sheet"] = (cs, "Porteria a 0 en los ultimos 3 de alguno de los dos equipos" if cs else "Sin porterias a 0 en los ultimos 3")
    else:
        results["clean_sheet"] = (False, "Datos insuficientes")

    if home_results and away_results:
        home_pts, away_pts = _form_points(home_results), _form_points(away_results)
        results["home_form"] = (home_pts > away_pts, f"Puntos recientes: local {home_pts} vs visitante {away_pts}")
    else:
        results["home_form"] = (False, "Datos insuficientes")

    ratio = _h2h_both_score_ratio(data.get("h2h"))
    if ratio:
        both, total = ratio
        results["h2h_goals"] = (both > total / 2, f"{both}/{total} enfrentamientos con ambos marcando")
    else:
        results["h2h_goals"] = (False, "Datos insuficientes")

    items = []
    score = 0
    max_pts = 0
    for c in conditions:
        status, reason = results.get(c["id"], (False, "Datos insuficientes"))
        max_pts += c["weight"]
        if status:
            score += c["weight"]
        items.append({"id": c["id"], "label": c["label"], "weight": c["weight"], "status": status, "reason": reason})

    pct = round(100 * score / max_pts) if max_pts else 0
    if pct >= 70:
        verdict = "FAVORABLE"
    elif pct >= 50:
        verdict = "DUDOSO"
    else:
        verdict = "NO RECOMENDABLE"

    return {"items": items, "score": score, "max_pts": max_pts, "pct": pct, "verdict": verdict, "stat_probs": stat_probs}


# Prompt builder (solo se usa cuando NO hay datos propios ni de APIs -- ver
# _render_report_no_data -- porque en ese caso no hay nada fiable que
# precalcular y el LLM tiene que buscar y redactar el informe entero)

def build_prompt(home: str, away: str, conditions: list[dict], data: dict, match_date: str | None = None) -> str:
    now = datetime.now().strftime("%d/%m/%Y")
    # Fecha mostrada junto al nombre de la competicion: debe ser la fecha REAL
    # del kickoff del partido, no la fecha en que corre el analisis. Con la
    # ventana de 30h (ver get_upcoming_fixtures en scheduler.py) un partido
    # analizado "hoy" puede jugarse manana, y mostrar "hoy" ahi es enganoso
    # (bug reportado: "Dalian Zhixing" no aparecia en la agenda del dia).
    display_date = match_date or now
    max_pts = sum(c["weight"] for c in conditions)
    hd = data.get("home_data") or {}
    ad = data.get("away_data") or {}

    blocks = [f"=== DATOS REALES ({now}) ===\n"]

    def team_block(team_info, td, name, role):
        if not td:
            return f"{name.upper()} - Sin datos disponibles"
        tname = name
        if team_info:
            tname = team_info.get("team", {}).get("name") or team_info.get("name") or name

        loc_data  = td.get("home" if role=="home" else "away", {})
        away_data = td.get("away" if role=="home" else "home", {})

        res_str = " ".join(loc_data.get("results", [])) or "sin datos"
        gf = nd(loc_data.get("gf"))
        ga = nd(loc_data.get("ga"))

        extras = []
        if loc_data.get("corners") is not None:
            extras.append(f"Corners: {loc_data['corners']}")
        if loc_data.get("shots") is not None:
            extras.append(f"Disparos: {loc_data['shots']}")
        if loc_data.get("cards") is not None:
            extras.append(f"Tarjetas: {loc_data['cards']}")
        extras_str = " | " + " | ".join(extras) if extras else ""

        away_gf = nd(away_data.get("gf"))
        away_ga = nd(away_data.get("ga"))
        away_res = " ".join(away_data.get("results", [])) or "sin datos"

        loc_label = "En casa" if role == "home" else "De visitante"
        away_label = "De visitante" if role == "home" else "En casa"

        src = td.get("source", "")
        src_note = f" [{src}]" if src else ""

        return (
            f"*{tname}*{src_note}\n"
            f"  {loc_label}: {res_str}\n"
            f"  Media goles: {gf} marc / {ga} enc{extras_str}\n"
            f"  {away_label}: {away_res} | Media: {away_gf} marc / {away_ga} enc"
        )

    blocks.append(team_block(data.get("home_team"), hd, home, "home"))
    blocks.append("\n" + team_block(data.get("away_team"), ad, away, "away"))

    if data.get("h2h"):
        h2h_lines = []
        for fix in data["h2h"][:3]:
            d = fix["fixture"]["date"][:10]
            gh = fix["goals"]["home"]
            ga = fix["goals"]["away"]
            hn2 = fix["teams"]["home"]["name"][:8]
            an2 = fix["teams"]["away"]["name"][:8]
            h2h_lines.append(f"{d} {hn2} {gh}-{ga} {an2}")
        blocks.append("\nH2H: " + " | ".join(h2h_lines))

    data_str = "\n".join(blocks)

    confidence = data.get("confidence", "low")
    if data["api_ok"]:
        web_instruction = ""
    else:
        web_instruction = (
            "\nSin datos en APIs. Usa web_search:\n"
            f"1. \"sofascore {home} resultados 2026\"\n"
            f"2. \"sofascore {away} resultados 2026\"\n"
            f"3. \"{home} {away} head to head\"\n"
        )

    if confidence == "high":
        confidence_banner = ""
        confidence_footer = f"_{data.get('source', 'Base de datos propia')} - {now}_"
    elif confidence == "medium":
        confidence_banner = "DATOS PARCIALES: solo un equipo con datos completos. Evalua condiciones con cautela.\n\n"
        confidence_footer = f"_Datos parciales - {data.get('source', '')} - {now}_"
    else:
        confidence_banner = "DATOS NO VERIFICADOS: sin cobertura suficiente. Analisis basado en busqueda web, tomalo con precaucion.\n\n"
        confidence_footer = f"_Datos no verificados - Busqueda web - {now}_"

    cond_list = "\n".join(f'- {c["label"]} (peso {c["weight"]})' for c in conditions)

    prompt_parts = []
    prompt_parts.append("Analista deportivo. Analisis BREVE para Telegram. Maximo 1800 caracteres.")
    prompt_parts.append("")
    prompt_parts.append("REGLA CRITICA: USA SOLO los datos proporcionados. NUNCA inventes porcentajes ni promedios.")
    prompt_parts.append("Si no tienes un dato, no lo menciones.")
    prompt_parts.append(f"Nivel de confianza de los datos: {confidence.upper()}")
    prompt_parts.append(web_instruction)
    prompt_parts.append("DATOS:")
    prompt_parts.append(data_str)
    prompt_parts.append("")
    prompt_parts.append("CONDICIONES A EVALUAR:")
    prompt_parts.append(cond_list)
    prompt_parts.append("")
    prompt_parts.append("FORMATO EXACTO:")
    prompt_parts.append("")
    prompt_parts.append(confidence_banner + f"*{home.upper()} vs {away.upper()}*")
    prompt_parts.append(f"_[competicion] - {display_date}_")
    prompt_parts.append("")
    prompt_parts.append(f"*{home}* - [resultados x5 casa en una linea]")
    prompt_parts.append("Goles casa: X marc / X enc | Corners: X | Disparos: X | Tarj: X (omite si no hay dato)")
    prompt_parts.append("")
    prompt_parts.append(f"*{away}* - [resultados x5 fuera en una linea]")
    prompt_parts.append("Goles fuera: X marc / X enc | Corners: X | Disparos: X | Tarj: X (omite si no hay dato)")
    prompt_parts.append("")
    prompt_parts.append("*H2H* - [ultimos 3] - media goles: X")
    prompt_parts.append("")
    prompt_parts.append("----------------")
    prompt_parts.append("*Condiciones*")
    prompt_parts.append("[cada una en UNA linea: si/no Nombre - motivo basado SOLO en datos reales]")
    prompt_parts.append("")
    prompt_parts.append(f"*X/{max_pts} pts - X%*")
    prompt_parts.append("FAVORABLE / DUDOSO / NO RECOMENDABLE")
    prompt_parts.append("")
    prompt_parts.append("[1 frase conclusion]")
    prompt_parts.append(confidence_footer)

    return "\n".join(prompt_parts)


# ─────────────────────────────────────────────────────────────────────────
# Redaccion del informe final
# ─────────────────────────────────────────────────────────────────────────
# El contenido numerico (datos, condiciones, puntuacion, veredicto) ya esta
# decidido por evaluate_conditions. El LLM solo entra para dos cosas que de
# verdad requieren "saber algo" en vez de calcular: el nombre de la
# competicion (si la conoce) y una frase de conclusion. Prompt y max_tokens
# minimos porque no necesita ver el volcado de datos completo ni reglas
# anti-invencion de estadisticas -- no calcula nada.

def _format_team_report_line(name: str, td: dict | None, role: str) -> str:
    if not td:
        return f"*{name}* - sin datos disponibles"
    own = td.get(role, {}) or {}
    res_str = " ".join(own.get("results", [])) or "sin datos"
    gf, ga = nd(own.get("gf")), nd(own.get("ga"))
    extras = []
    if own.get("corners") is not None:
        extras.append(f"Corners: {own['corners']}")
    if own.get("shots") is not None:
        extras.append(f"Disparos: {own['shots']}")
    if own.get("cards") is not None:
        extras.append(f"Tarj: {own['cards']}")
    extras_str = (" | " + " | ".join(extras)) if extras else ""
    label = "casa" if role == "home" else "fuera"
    return f"*{name}* - {res_str}\nGoles {label}: {gf} marc / {ga} enc{extras_str}"


def _format_h2h_line(h2h: list | None) -> str:
    if not h2h:
        return "*H2H* - sin enfrentamientos recientes en las fuentes disponibles"
    lines, goals = [], []
    for fix in h2h[:3]:
        gh, ga = fix["goals"]["home"], fix["goals"]["away"]
        d = fix["fixture"]["date"][:10]
        hn = fix["teams"]["home"]["name"][:8]
        an = fix["teams"]["away"]["name"][:8]
        lines.append(f"{d} {hn} {gh}-{ga} {an}")
        if gh is not None and ga is not None:
            goals.append(gh + ga)
    return f"*H2H* - {' | '.join(lines)} - media goles: {nd(avg(goals))}"


def _format_condition_lines(evaluation: dict) -> str:
    lines = []
    for item in evaluation["items"]:
        mark = "Si" if item["status"] else "No"
        lines.append(f"{mark} {item['label']} - {item['reason']}")
    return "\n".join(lines)


def _confidence_banner_footer(data: dict, now: str) -> tuple[str, str]:
    confidence = data.get("confidence", "low")
    if confidence == "high":
        return "", f"_{data.get('source', 'Base de datos propia')} - {now}_"
    if confidence == "medium":
        return (
            "DATOS PARCIALES: solo un equipo con datos completos. Evalua las condiciones con cautela.\n\n",
            f"_Datos parciales - {data.get('source', '')} - {now}_",
        )
    return (
        "DATOS NO VERIFICADOS: sin cobertura suficiente para un analisis fiable.\n\n",
        f"_Datos no verificados - {now}_",
    )


async def _ask_competition_and_conclusion(home: str, away: str, evaluation: dict, confidence: str) -> tuple[str, str]:
    """Unica llamada al LLM del flujo normal (con datos propios). No decide
    ninguna cifra ni ningun si/no: solo aporta el nombre de la competicion
    (si la conoce) y una frase de conclusion corta. Si falla, se devuelve
    ("", "") y _render_report cae a una conclusion generica basada en el
    veredicto ya calculado -- el informe nunca se rompe por esto."""
    cumplen = [it["label"] for it in evaluation["items"] if it["status"]]
    no_cumplen = [it["label"] for it in evaluation["items"] if not it["status"]]

    prompt = (
        f"Partido: {home} vs {away}\n"
        f"Resultado YA CALCULADO (no lo recalcules, solo redacta): "
        f"{evaluation['score']}/{evaluation['max_pts']} pts ({evaluation['pct']}%) -> {evaluation['verdict']}\n"
        f"Confianza de los datos: {confidence}\n"
        f"Condiciones que SI se cumplen: {', '.join(cumplen) or 'ninguna'}\n"
        f"Condiciones que NO se cumplen: {', '.join(no_cumplen) or 'ninguna'}\n\n"
        "Responde EXCLUSIVAMENTE con un JSON de una linea, sin bloque de codigo ni texto adicional:\n"
        '{"competition":"<liga o torneo del partido si lo conoces, si no cadena vacia>",'
        '"conclusion":"<1 frase breve en espanol resumiendo el mercado mas avalado por el veredicto>"}'
    )
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 150,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
        "system": "Devuelves siempre JSON valido de una sola linea, sin texto extra ni bloques de codigo.",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            data_r = r.json()
            text = "".join(b["text"] for b in data_r.get("content", []) if b.get("type") == "text").strip()
            text = re.sub(r"^```(json)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
            parsed = json.loads(text)
            return str(parsed.get("competition") or "").strip(), str(parsed.get("conclusion") or "").strip()
    except Exception as e:
        print(f"[DEBUG] _ask_competition_and_conclusion FALLO: {type(e).__name__}: {e}")
        return "", ""


async def _render_report(home: str, away: str, data: dict, evaluation: dict, match_date: str | None = None) -> str:
    now = datetime.now().strftime("%d/%m/%Y")
    display_date = match_date or now
    confidence = data.get("confidence", "low")
    banner, footer = _confidence_banner_footer(data, now)

    competition, conclusion = await _ask_competition_and_conclusion(home, away, evaluation, confidence)
    comp_line = f"_{competition} - {display_date}_" if competition else f"_{display_date}_"
    if not conclusion:
        conclusion = f"Veredicto: {evaluation['verdict'].lower()} segun los datos disponibles."

    parts = [
        f"{banner}*{home.upper()} vs {away.upper()}*",
        comp_line,
        "",
        _format_team_report_line(home, data.get("home_data"), "home"),
        "",
        _format_team_report_line(away, data.get("away_data"), "away"),
        "",
        _format_h2h_line(data.get("h2h")),
        "",
        "----------------",
        "*Condiciones*",
        _format_condition_lines(evaluation),
        "",
        f"*{evaluation['score']}/{evaluation['max_pts']} pts - {evaluation['pct']}%*",
        evaluation["verdict"],
        "",
        conclusion,
        footer,
    ]
    return "\n".join(parts)


async def _render_report_no_data(home: str, away: str, conditions: list[dict], data: dict, match_date: str | None = None) -> str:
    """Sin datos propios ni de APIs (api_ok=False): no hay nada fiable que
    precalcular en Python, asi que mantenemos el flujo completo original --
    el LLM arma el informe entero apoyandose en web_search."""
    prompt = build_prompt(home, away, conditions, data, match_date)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en futbol. Respondes siempre en espanol. "
            "Usas SOLO los datos reales proporcionados. "
            "NUNCA inventes estadisticas, porcentajes ni promedios. "
            "Si no tienes un dato, no lo menciones. "
            "Formato Markdown Telegram. Respuestas concisas."
        ),
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
    }

    print(f"[DEBUG] Llamando a Anthropic (sin datos propios). ANTHROPIC_API_KEY presente: {bool(ANTHROPIC_API_KEY)} (len={len(ANTHROPIC_API_KEY)})")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            print(f"[DEBUG] Anthropic status_code: {r.status_code}")
            r.raise_for_status()
            data_r = r.json()
            text_parts = [
                block["text"]
                for block in data_r.get("content", [])
                if block.get("type") == "text"
            ]
            if not text_parts:
                print(f"[DEBUG] Respuesta de Anthropic sin texto. stop_reason={data_r.get('stop_reason')} content_types={[b.get('type') for b in data_r.get('content', [])]}")
            return "\n".join(text_parts) if text_parts else "No se pudo generar el analisis."
    except httpx.HTTPStatusError as e:
        print(f"[DEBUG] Anthropic HTTPStatusError: {e.response.status_code} - {e.response.text}")
        return "Error al generar el analisis. Intentalo de nuevo en unos segundos."
    except Exception as e:
        print(f"[DEBUG] Unexpected error: {type(e).__name__}: {e}")
        return "Error inesperado. Revisa los logs del servidor."


async def analyze_match(home: str, away: str, conditions: list[dict] | None = None, match_date: str | None = None) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    data = await build_real_data(home, away)

    if not data["api_ok"]:
        return await _render_report_no_data(home, away, conditions, data, match_date)

    evaluation = evaluate_conditions(home, away, conditions, data)
    return await _render_report(home, away, data, evaluation, match_date)


# ---------------------------------------------------------------------------
# Motor estadistico validado: Poisson (ataque/defensa) + calibracion Platt
# ---------------------------------------------------------------------------
# Los coeficientes de abajo (slope, intercept) se calcularon y VALIDARON con
# un split temporal estricto de 3 pasos, usando los partidos historicos
# (2023-2025) acumulados en la tabla "partidos":
#   1. Fuerza de ataque/defensa de cada equipo -> calculada SOLO con 2023-2024
#   2. Calibracion de Platt -> ajustada SOLO con 2025 (datos que el paso 1
#      nunca vio)
#   3. Prueba ciega -> comprobada contra 2026 (datos que ni el paso 1 ni el
#      paso 2 vieron nunca)
# Los tres mercados de abajo pasaron esa prueba ciega con una mejora clara
# del error de calibracion. Ver sesion de analisis "corners/goles/btts"
# para el detalle completo. home_goals, away_goals, tarjetas y el modelo
# combinado de BTTS se validaron y anadieron en sesiones posteriores (ver
# comentarios junto a cada constante mas abajo).
PLATT_BTTS = (0.1079, 0.3977)        # (slope, intercept)
PLATT_OVER25 = (0.2178, 0.4273)
PLATT_CORNERS85 = (0.2576, 0.5763)
PLATT_HOME_GOALS = (0.5742, 0.1132)  # validado, mejora moderada (~9.2pp -> ~6.1pp de error)

# BTTS con modelo COMBINADO (media de la lambda de goles y la lambda de
# tiros a puerta "traducida" a goles via tasa de conversion de la liga).
# Validado con split temporal riguroso Y con backtest real contra cuotas:
# mejora el acierto de 52.2% a 58.2% y reduce el error (Brier 0.2498 ->
# 0.2420) frente al modelo de solo goles. Ver sesion de analisis
# "modelo combinado BTTS". Solo se ha validado para BTTS -- Over 2.5 y el
# resto de mercados siguen usando el modelo de solo-goles de siempre.
PLATT_BTTS_COMBINADO = (0.4246, 0.1867)


# Tarjetas Over 3.5: antes descartada por ruidosa con muestra pequena
# (~930 partidos de calibracion). Con la ingesta historica completa
# (~2585 partidos de calibracion, casi el triple), la correccion mejora
# de forma limpia y consistente en TODOS los deciles (antes solo mejoraba
# en algunos). Validado con el mismo split temporal riguroso. Excluye
# liga_id=14 (Islandia, sin datos reales) y exige minimo 5 partidos por
# equipo para calcular su fuerza.
PLATT_CARDS35 = (0.5332, 0.0171)


# away_goals: coeficientes actualizados. La primera vez que se valido (con
# ~900 partidos de calibracion) la correccion no mejoraba nada frente a
# usar la probabilidad cruda. Con la ingesta historica completa (~2747
# partidos de calibracion, 3 veces mas), SI mejora de forma clara y
# consistente en los 5 deciles (error medio de ~6.3pp a ~1.7pp).
PLATT_AWAY_GOALS_V2 = (0.3662, -0.2752)

LIGA_ID_SIN_CORNERS = 14  # Urvalsdeild (Islandia): nunca ha tenido datos de corners/tarjetas


def _platt(p_raw: float, slope: float, intercept: float) -> float:
    """Aplica la recalibracion de Platt (regresion logistica de calibracion)
    a una probabilidad Poisson 'cruda'. p_raw se recorta a [0.01, 0.99] para
    que el logit no explote en los extremos."""
    p_raw = min(max(p_raw, 0.01), 0.99)
    logit = math.log(p_raw / (1 - p_raw))
    return 1 / (1 + math.exp(-(intercept + slope * logit)))


def _poisson_cdf_corners_le8(lt: float) -> float:
    """P(corners totales <= 8) via suma directa de la Poisson pmf, k=0..8."""
    pmf = math.exp(-lt)
    cdf = pmf
    for k in range(1, 9):
        pmf *= lt / k
        cdf += pmf
    return cdf


def _poisson_cdf_cards_le3(lt: float) -> float:
    """P(tarjetas totales <= 3) via suma directa de la Poisson pmf, k=0..3."""
    pmf = math.exp(-lt)
    cdf = pmf
    for k in range(1, 4):
        pmf *= lt / k
        cdf += pmf
    return cdf


def poisson_calibrated_probs(home: str, away: str) -> dict:
    """Calcula BTTS, Over 2.5 goles y Over 8.5 corners con el modelo Poisson
    de ataque/defensa (calculado sobre TODO el historico disponible en la
    tabla partidos) y les aplica la calibracion de Platt validada arriba.

    Devuelve un dict {condicion_id: probabilidad_0_a_100}. Una condicion se
    omite del dict si no hay datos suficientes para calcularla (equipo con
    pocos partidos en la BD, o liga sin datos de corners) -- en ese caso el
    pick de esa condicion sigue dependiendo de la estimacion de Claude, sin
    override.
    """
    result: dict[str, float] = {}
    if not DATABASE_URL:
        return result
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT liga_id, count(*) as n FROM (
                SELECT liga_id FROM partidos WHERE equipo_local ILIKE %s
                UNION ALL
                SELECT liga_id FROM partidos WHERE equipo_visitante ILIKE %s
            ) t GROUP BY liga_id ORDER BY n DESC LIMIT 1
        """, (f"%{home}%", f"%{home}%"))
        row = cur.fetchone()
        if not row:
            conn.close()
            return result
        liga_id = row[0]

        # --- Goles: BTTS y Over 2.5 ---
        cur.execute(
            "SELECT avg(goles_local), avg(goles_visitante) FROM partidos WHERE liga_id=%s",
            (liga_id,)
        )
        avg_home_g, avg_away_g = cur.fetchone()

        def _goal_strengths(team):
            cur.execute("""
                SELECT avg(goles_local), avg(goles_visitante), count(*)
                FROM partidos WHERE liga_id=%s AND equipo_local ILIKE %s
            """, (liga_id, f"%{team}%"))
            gf_h, ga_h, n_h = cur.fetchone()
            cur.execute("""
                SELECT avg(goles_visitante), avg(goles_local), count(*)
                FROM partidos WHERE liga_id=%s AND equipo_visitante ILIKE %s
            """, (liga_id, f"%{team}%"))
            gf_a, ga_a, n_a = cur.fetchone()
            if gf_h is None or gf_a is None or n_h < 3 or n_a < 3:
                return None
            return float(gf_h), float(ga_h), float(gf_a), float(ga_a)

        def _shot_strengths(team):
            """Igual que _goal_strengths pero con tiros a puerta -- se usa
            SOLO para el modelo combinado de BTTS (ver PLATT_BTTS_COMBINADO)."""
            cur.execute("""
                SELECT avg(tiros_puerta_local), avg(tiros_puerta_visitante), count(*)
                FROM partidos WHERE liga_id=%s AND equipo_local ILIKE %s
                AND NOT (tiros_puerta_local=0 AND tiros_puerta_visitante=0)
            """, (liga_id, f"%{team}%"))
            sf_h, sa_h, n_h = cur.fetchone()
            cur.execute("""
                SELECT avg(tiros_puerta_visitante), avg(tiros_puerta_local), count(*)
                FROM partidos WHERE liga_id=%s AND equipo_visitante ILIKE %s
                AND NOT (tiros_puerta_local=0 AND tiros_puerta_visitante=0)
            """, (liga_id, f"%{team}%"))
            sf_a, sa_a, n_a = cur.fetchone()
            if sf_h is None or sf_a is None or n_h < 3 or n_a < 3:
                return None
            return float(sf_h), float(sa_h), float(sf_a), float(sa_a)

        if avg_home_g and avg_away_g:
            home_gs = _goal_strengths(home)
            away_gs = _goal_strengths(away)
            if home_gs and away_gs:
                gf_home_h, ga_home_h, _, _ = home_gs
                _, _, gf_away_a, ga_away_a = away_gs
                avg_home_g, avg_away_g = float(avg_home_g), float(avg_away_g)

                attack_home = gf_home_h / avg_home_g
                defense_home = ga_home_h / avg_away_g
                attack_away = gf_away_a / avg_away_g
                defense_away = ga_away_a / avg_home_g

                lh = avg_home_g * attack_home * defense_away
                lav = avg_away_g * attack_away * defense_home

                lt = lh + lav
                p_over25_raw = 1 - math.exp(-lt) * (1 + lt + (lt ** 2) / 2)
                result["over25"] = round(_platt(p_over25_raw, *PLATT_OVER25) * 100, 1)

                p_home15_raw = 1 - math.exp(-lh) * (1 + lh)
                result["home_goals"] = round(_platt(p_home15_raw, *PLATT_HOME_GOALS) * 100, 1)

                p_away15_raw = 1 - math.exp(-lav) * (1 + lav)
                result["away_goals"] = round(_platt(p_away15_raw, *PLATT_AWAY_GOALS_V2) * 100, 1)

                # BTTS: modelo combinado (goles + tiros a puerta) si hay
                # datos de tiros a puerta disponibles; si no, cae al
                # modelo de solo goles (con su propia calibracion) para no
                # dejar el mercado sin cubrir.
                cur.execute("""
                    SELECT avg(tiros_puerta_local), avg(tiros_puerta_visitante) FROM partidos
                    WHERE liga_id=%s AND NOT (tiros_puerta_local=0 AND tiros_puerta_visitante=0)
                """, (liga_id,))
                avg_home_s, avg_away_s = cur.fetchone()
                home_ss = _shot_strengths(home) if avg_home_s and avg_away_s else None
                away_ss = _shot_strengths(away) if avg_home_s and avg_away_s else None

                if home_ss and away_ss:
                    sf_home_h, sa_home_h, _, _ = home_ss
                    _, _, sf_away_a, sa_away_a = away_ss
                    avg_home_s, avg_away_s = float(avg_home_s), float(avg_away_s)

                    attack_home_s = sf_home_h / avg_home_s
                    defense_home_s = sa_home_h / avg_away_s
                    attack_away_s = sf_away_a / avg_away_s
                    defense_away_s = sa_away_a / avg_home_s

                    conv_home = avg_home_g / avg_home_s
                    conv_away = avg_away_g / avg_away_s
                    lh_s = avg_home_s * attack_home_s * defense_away_s * conv_home
                    lav_s = avg_away_s * attack_away_s * defense_home_s * conv_away

                    lh_comb = (lh + lh_s) / 2
                    lav_comb = (lav + lav_s) / 2
                    p_btts_raw = (1 - math.exp(-lh_comb)) * (1 - math.exp(-lav_comb))
                    result["btts"] = round(_platt(p_btts_raw, *PLATT_BTTS_COMBINADO) * 100, 1)
                else:
                    p_btts_raw = (1 - math.exp(-lh)) * (1 - math.exp(-lav))
                    result["btts"] = round(_platt(p_btts_raw, *PLATT_BTTS) * 100, 1)

        # --- Corners: Over 8.5 (excluye liga sin datos y filas 0-0 contaminadas) ---
        if liga_id != LIGA_ID_SIN_CORNERS:
            cur.execute("""
                SELECT avg(corners_local), avg(corners_visitante) FROM partidos
                WHERE liga_id=%s AND NOT (corners_local=0 AND corners_visitante=0)
            """, (liga_id,))
            avg_home_c, avg_away_c = cur.fetchone()

            def _corner_strengths(team):
                cur.execute("""
                    SELECT avg(corners_local), avg(corners_visitante), count(*)
                    FROM partidos WHERE liga_id=%s AND equipo_local ILIKE %s
                    AND NOT (corners_local=0 AND corners_visitante=0)
                """, (liga_id, f"%{team}%"))
                cf_h, ca_h, n_h = cur.fetchone()
                cur.execute("""
                    SELECT avg(corners_visitante), avg(corners_local), count(*)
                    FROM partidos WHERE liga_id=%s AND equipo_visitante ILIKE %s
                    AND NOT (corners_local=0 AND corners_visitante=0)
                """, (liga_id, f"%{team}%"))
                cf_a, ca_a, n_a = cur.fetchone()
                if cf_h is None or cf_a is None or n_h < 3 or n_a < 3:
                    return None
                return float(cf_h), float(ca_h), float(cf_a), float(ca_a)

            if avg_home_c and avg_away_c:
                home_cs = _corner_strengths(home)
                away_cs = _corner_strengths(away)
                if home_cs and away_cs:
                    cf_home_h, ca_home_h, _, _ = home_cs
                    _, _, cf_away_a, ca_away_a = away_cs
                    avg_home_c, avg_away_c = float(avg_home_c), float(avg_away_c)

                    attack_home_c = cf_home_h / avg_home_c
                    defense_home_c = ca_home_h / avg_away_c
                    attack_away_c = cf_away_a / avg_away_c
                    defense_away_c = ca_away_a / avg_home_c

                    lh_c = avg_home_c * attack_home_c * defense_away_c
                    lav_c = avg_away_c * attack_away_c * defense_home_c
                    lt_c = lh_c + lav_c

                    p_over85_raw = 1 - _poisson_cdf_corners_le8(lt_c)
                    result["corners_over85"] = round(_platt(p_over85_raw, *PLATT_CORNERS85) * 100, 1)

        # --- Tarjetas: Over 3.5 (excluye liga sin datos y exige minimo 5
        # partidos por equipo para calcular su fuerza, igual que se
        # valido) ---
        if liga_id != LIGA_ID_SIN_CORNERS:
            cur.execute("""
                SELECT avg(tarjetas_amarillas_local+tarjetas_rojas_local),
                       avg(tarjetas_amarillas_visitante+tarjetas_rojas_visitante)
                FROM partidos WHERE liga_id=%s
            """, (liga_id,))
            avg_home_j, avg_away_j = cur.fetchone()

            def _card_strengths(team):
                cur.execute("""
                    SELECT avg(tarjetas_amarillas_local+tarjetas_rojas_local),
                           avg(tarjetas_amarillas_visitante+tarjetas_rojas_visitante), count(*)
                    FROM partidos WHERE liga_id=%s AND equipo_local ILIKE %s
                """, (liga_id, f"%{team}%"))
                jf_h, ja_h, n_h = cur.fetchone()
                cur.execute("""
                    SELECT avg(tarjetas_amarillas_visitante+tarjetas_rojas_visitante),
                           avg(tarjetas_amarillas_local+tarjetas_rojas_local), count(*)
                    FROM partidos WHERE liga_id=%s AND equipo_visitante ILIKE %s
                """, (liga_id, f"%{team}%"))
                jf_a, ja_a, n_a = cur.fetchone()
                if jf_h is None or jf_a is None or n_h < 5 or n_a < 5:
                    return None
                return float(jf_h), float(ja_h), float(jf_a), float(ja_a)

            if avg_home_j and avg_away_j:
                home_js = _card_strengths(home)
                away_js = _card_strengths(away)
                if home_js and away_js:
                    jf_home_h, ja_home_h, _, _ = home_js
                    _, _, jf_away_a, ja_away_a = away_js
                    avg_home_j, avg_away_j = float(avg_home_j), float(avg_away_j)

                    attack_home_j = jf_home_h / avg_home_j
                    defense_home_j = ja_home_h / avg_away_j
                    attack_away_j = jf_away_a / avg_away_j
                    defense_away_j = ja_away_a / avg_home_j

                    lh_j = avg_home_j * attack_home_j * defense_away_j
                    lav_j = avg_away_j * attack_away_j * defense_home_j
                    lt_j = max(lh_j + lav_j, 0.05)

                    p_cards35_raw = 1 - _poisson_cdf_cards_le3(lt_j)
                    result["cards_over35"] = round(_platt(p_cards35_raw, *PLATT_CARDS35) * 100, 1)

        conn.close()
        return result
    except Exception as e:
        print(f"[DEBUG] poisson_calibrated_probs({home} vs {away}) FALLO: {type(e).__name__}: {e}")
        return result


# Numero minimo de partidos de historial (casa Y fuera) que exigimos antes de
# dejar que un partido aporte picks al ranking diario. Con menos muestra que
# esto, un pick de "alta confianza" es enganoso (ej. Kaizer Chiefs con 1 solo
# partido de referencia) aunque el porcentaje parezca solido.
MIN_MATCHES_FOR_PICK = 3


def _derive_deterministic_picks(conditions: list[dict], data: dict, evaluation: dict) -> list[dict]:
    """Picks para el ranking diario de mejores partidos: SOLO los mercados
    con modelo estadistico validado (Poisson + calibracion Platt, ver
    poisson_calibrated_probs). El resto de condiciones de evaluate_conditions
    (home_form, h2h_goals, etc.) no tiene un numero calibrado detras --
    mezclarlas aqui distorsionaria la comparativa entre partidos del dia,
    aunque si aparecen en el informe individual de cada partido.

    Antes esto se lo pediamos a Claude en un JSON aparte y despues se
    validaba/sobreescribia contra estos mismos numeros calculados en Python
    (ver commits anteriores). Ahora se usa el numero validado directamente,
    sin pasar por el LLM en absoluto -- ni el calculo ni la validacion
    posterior hacian falta ya."""
    home_data = (data.get("home_data") or {}).get("home", {}) or {}
    away_data = (data.get("away_data") or {}).get("away", {}) or {}
    home_n = len(home_data.get("results") or [])
    away_n = len(away_data.get("results") or [])

    if home_n < MIN_MATCHES_FOR_PICK or away_n < MIN_MATCHES_FOR_PICK:
        print(f"[DEBUG] _derive_deterministic_picks: muestra insuficiente "
              f"(casa={home_n}, fuera={away_n}, minimo={MIN_MATCHES_FOR_PICK}) - se descartan todos los picks")
        return []

    labels = {c["id"]: c["label"] for c in conditions}
    sample_str = f"{home_n} casa / {away_n} fuera"
    stat_probs = evaluation.get("stat_probs") or {}

    return [
        {
            "id": cond_id,
            "label": labels[cond_id],
            "probability": prob,
            "reason": "Calculado con el modelo estadistico validado (Poisson + calibracion Platt).",
            "sample": sample_str,
        }
        for cond_id, prob in stat_probs.items()
        if cond_id in labels and prob >= 60
    ]


async def analyze_match_with_picks(home: str, away: str, conditions: list[dict] | None = None, match_date: str | None = None) -> tuple[str, list[dict]]:
    """Como analyze_match(), pero ademas devuelve picks estructurados
    [{"id","label","probability","reason","sample"}, ...] para el ranking
    diario de mejores partidos. Los picks salen directamente del modelo
    estadistico validado (ver _derive_deterministic_picks), sin ninguna
    llamada adicional al LLM -- comparten los mismos datos y la misma
    evaluacion que ya calcula el informe, asi que no cuestan tokens extra
    ni tiempo extra.

    match_date (opcional): fecha del kickoff en formato DD/MM/YYYY, ya
    convertida a hora Espana por quien llama (ver scheduler.py). Si no se
    pasa, el informe usa la fecha de hoy como antes."""
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    data = await build_real_data(home, away)

    if not data["api_ok"]:
        report = await _render_report_no_data(home, away, conditions, data, match_date)
        return report, []

    evaluation = evaluate_conditions(home, away, conditions, data)
    report = await _render_report(home, away, data, evaluation, match_date)
    picks = _derive_deterministic_picks(conditions, data, evaluation)
    return report, picks
