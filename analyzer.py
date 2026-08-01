import os
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
    {"id": "away_concede", "label": "Visitante encaja en todos sus partidos fuera",  "weight": 4},
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

# Prompt builder

def build_prompt(home: str, away: str, conditions: list[dict], data: dict) -> str:
    now = datetime.now().strftime("%d/%m/%Y")
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
    prompt_parts.append(f"_[competicion] - {now}_")
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


async def analyze_match(home: str, away: str, conditions: list[dict] | None = None) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    data = await build_real_data(home, away)

    prompt = build_prompt(home, away, conditions, data)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en futbol. Respondes siempre en espanol. "
            "Usas SOLO los datos reales proporcionados. "
            "NUNCA inventes estadisticas, porcentajes ni promedios. "
            "Si no tienes un dato, no lo menciones. "
            "Formato Markdown Telegram. Respuestas concisas."
        ),
    }

    if not data["api_ok"]:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]

    print(f"[DEBUG] Llamando a Anthropic. ANTHROPIC_API_KEY presente: {bool(ANTHROPIC_API_KEY)} (len={len(ANTHROPIC_API_KEY)})")

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
