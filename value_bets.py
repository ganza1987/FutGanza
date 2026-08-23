"""
value_bets.py

Automatiza lo que hasta ahora haciamos a mano en el editor SQL de Supabase:

1. Calcula la probabilidad de cada partido para 5 mercados ya validados
   (BTTS, Over 2.5 goles, Local +1.5, Visitante +1.5, Corners +8.5) usando
   el mismo modelo Poisson + calibracion de Platt que ya validamos con
   split temporal riguroso.
2. Cruza esa probabilidad con las cuotas guardadas en la tabla "cuotas":
   quita el margen de la casa (vig) usando el par Yes/No (o Over/Under),
   calcula el consenso (mediana) entre todas las casas disponibles, y la
   mejor cuota individual para saber donde apostarias.
3. Marca como "pick de valor" cualquier combinacion donde nuestra
   probabilidad supere el consenso del mercado por un margen de seguridad
   (15 puntos porcentuales por defecto -- ver MARGEN_SEGURIDAD_PP).

Dos modos de uso:

    python value_bets.py
        Modo EN VIVO: busca picks de valor entre los partidos que tienen
        cuotas guardadas pero SIN resultado todavia (partidos futuros o en
        juego). Pensado para ejecutarse a diario junto al resto de tareas
        programadas.

    python value_bets.py --backtest
        Modo BACKTEST: usa unicamente partidos que YA tienen resultado
        final Y cuotas guardadas, para medir que rendimiento (ROI, %
        acierto) habria dado el sistema si hubieras apostado siempre que
        detectaba valor. Se recomienda repetir este modo de vez en cuando,
        segun se acumulen mas dias de cuotas guardadas -- con pocos casos
        (como ahora) el resultado no es fiable estadisticamente todavia.

Variables de entorno necesarias: DATABASE_URL (las mismas que ya usas en
el resto del proyecto).
"""

import os
import sys
import math
import argparse
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Liga sin datos reales de corners/tarjetas (Urvalsdeild, Islandia) --
# se excluye siempre de esos mercados. Ver sesion de analisis de corners.
LIGA_ID_SIN_CORNERS = 14

# Corte de fecha para separar "pasado" (con el que se calculan las fuerzas
# de ataque/defensa) del resto. Por defecto, hoy -- es decir, se usa TODO
# el historico disponible para calcular fuerzas, tal como hace el bot en
# produccion. En modo --backtest, este corte se ajusta automaticamente
# para no hacer trampa (nunca se usan partidos del propio periodo de
# prueba para calcular las fuerzas de los equipos).
MARGEN_SEGURIDAD_PP = 15.0  # puntos porcentuales de ventaja minima exigida

# Coeficientes de calibracion de Platt (slope, intercept), validados con
# split temporal riguroso (fuerza 2023-24 -> calibracion 2025 -> prueba
# ciega en 2026). Ver sesion "corners/goles/btts".
PLATT = {
    "btts": (0.1079, 0.3977),
    "over25": (0.2178, 0.4273),
    "home_goals": (0.5742, 0.1132),
    "away_goals": (0.2156, -0.1592),
    "corners_over85": (0.2576, 0.5763),
}

# Mapeo condicion -> (nombre de mercado en la cuota, valor a buscar, valor opuesto)
MERCADOS = {
    "btts": ("Both Teams Score", "Yes", "No"),
    "over25": ("Goals Over/Under", "Over 2.5", "Under 2.5"),
    "home_goals": ("Total - Home", "Over 1.5", "Under 1.5"),
    "away_goals": ("Total - Away", "Over 1.5", "Under 1.5"),
    "corners_over85": ("Corners Over Under", "Over 8.5", "Under 8.5"),
}


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no configurada")
    return psycopg2.connect(DATABASE_URL)


def _platt(p_raw: float, slope: float, intercept: float) -> float:
    p_raw = min(max(p_raw, 0.01), 0.99)
    logit = math.log(p_raw / (1 - p_raw))
    return 1 / (1 + math.exp(-(intercept + slope * logit)))


def _poisson_cdf(lam: float, k_max: int) -> float:
    """P(X <= k_max) para X ~ Poisson(lam)."""
    pmf = math.exp(-lam)
    cdf = pmf
    for k in range(1, k_max + 1):
        pmf *= lam / k
        cdf += pmf
    return cdf


# ---------------------------------------------------------------------------
# PASO 1: fuerzas de ataque/defensa (goles y corners) a partir del historico
# ---------------------------------------------------------------------------

def calcular_fuerzas(conn, fecha_corte: str | None = None):
    """Calcula, para cada liga y equipo, las fuerzas de ataque/defensa de
    goles y de corners, usando todos los partidos anteriores a fecha_corte
    (o todo el historico si no se especifica). Devuelve un diccionario con
    todo lo necesario para predecir cualquier partido futuro."""
    where_fecha = "WHERE fecha < %s" if fecha_corte else ""
    params = (fecha_corte,) if fecha_corte else ()

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # --- Goles ---
    cur.execute(f"""
        SELECT liga_id, avg(goles_local) as avg_home, avg(goles_visitante) as avg_away
        FROM partidos {where_fecha} GROUP BY liga_id
    """, params)
    liga_avg_goles = {r["liga_id"]: (float(r["avg_home"]), float(r["avg_away"])) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT liga_id, equipo_local as team, avg(goles_local) as gf, avg(goles_visitante) as ga
        FROM partidos {where_fecha} GROUP BY liga_id, equipo_local
    """, params)
    team_home_goles = {(r["liga_id"], r["team"]): (float(r["gf"]), float(r["ga"])) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT liga_id, equipo_visitante as team, avg(goles_visitante) as gf, avg(goles_local) as ga
        FROM partidos {where_fecha} GROUP BY liga_id, equipo_visitante
    """, params)
    team_away_goles = {(r["liga_id"], r["team"]): (float(r["gf"]), float(r["ga"])) for r in cur.fetchall()}

    # --- Corners (excluye filas 0-0 contaminadas) ---
    where_corners = where_fecha + (" AND" if where_fecha else "WHERE") + \
        " NOT (corners_local=0 AND corners_visitante=0)"
    cur.execute(f"""
        SELECT liga_id, avg(corners_local) as avg_home, avg(corners_visitante) as avg_away
        FROM partidos {where_corners} GROUP BY liga_id
    """, params)
    liga_avg_corners = {r["liga_id"]: (float(r["avg_home"]), float(r["avg_away"])) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT liga_id, equipo_local as team, avg(corners_local) as gf, avg(corners_visitante) as ga
        FROM partidos {where_corners} GROUP BY liga_id, equipo_local
    """, params)
    team_home_corners = {(r["liga_id"], r["team"]): (float(r["gf"]), float(r["ga"])) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT liga_id, equipo_visitante as team, avg(corners_visitante) as gf, avg(corners_local) as ga
        FROM partidos {where_corners} GROUP BY liga_id, equipo_visitante
    """, params)
    team_away_corners = {(r["liga_id"], r["team"]): (float(r["gf"]), float(r["ga"])) for r in cur.fetchall()}

    return {
        "liga_avg_goles": liga_avg_goles,
        "team_home_goles": team_home_goles,
        "team_away_goles": team_away_goles,
        "liga_avg_corners": liga_avg_corners,
        "team_home_corners": team_home_corners,
        "team_away_corners": team_away_corners,
    }


def predecir_partido(fuerzas: dict, liga_id: int, home: str, away: str) -> dict | None:
    """Calcula las 5 probabilidades calibradas para un partido, dado el
    diccionario de fuerzas ya calculado. Devuelve None si falta algun dato
    (equipo nuevo, liga sin datos suficientes, etc.) -- nunca inventa un
    numero a medias."""
    la_g = fuerzas["liga_avg_goles"].get(liga_id)
    th_g = fuerzas["team_home_goles"].get((liga_id, home))
    ta_g = fuerzas["team_away_goles"].get((liga_id, away))
    if not (la_g and th_g and ta_g):
        return None

    avg_home_g, avg_away_g = la_g
    gf_home, _ = th_g
    _, ga_away = ta_g
    attack_home = gf_home / avg_home_g
    defense_away = ga_away / avg_home_g
    ta_full = fuerzas["team_away_goles"].get((liga_id, away))
    th_full = fuerzas["team_home_goles"].get((liga_id, home))
    attack_away = ta_full[0] / avg_away_g
    defense_home = th_full[1] / avg_away_g

    lh = avg_home_g * attack_home * defense_away
    lav = avg_away_g * attack_away * defense_home

    p_btts_raw = (1 - math.exp(-lh)) * (1 - math.exp(-lav))
    lt = lh + lav
    p_over25_raw = 1 - math.exp(-lt) * (1 + lt + (lt ** 2) / 2)
    p_home15_raw = 1 - math.exp(-lh) * (1 + lh)
    p_away15_raw = 1 - math.exp(-lav) * (1 + lav)

    resultado = {
        "btts": round(_platt(p_btts_raw, *PLATT["btts"]) * 100, 1),
        "over25": round(_platt(p_over25_raw, *PLATT["over25"]) * 100, 1),
        "home_goals": round(_platt(p_home15_raw, *PLATT["home_goals"]) * 100, 1),
        "away_goals": round(_platt(p_away15_raw, *PLATT["away_goals"]) * 100, 1),
    }

    if liga_id != LIGA_ID_SIN_CORNERS:
        la_c = fuerzas["liga_avg_corners"].get(liga_id)
        th_c = fuerzas["team_home_corners"].get((liga_id, home))
        ta_c = fuerzas["team_away_corners"].get((liga_id, away))
        if la_c and th_c and ta_c:
            avg_home_c, avg_away_c = la_c
            attack_home_c = th_c[0] / avg_home_c
            defense_away_c = ta_c[1] / avg_home_c
            attack_away_c = ta_c[0] / avg_away_c
            defense_home_c = th_c[1] / avg_away_c
            lhc = avg_home_c * attack_home_c * defense_away_c
            lavc = avg_away_c * attack_away_c * defense_home_c
            ltc = lhc + lavc
            p_corners85_raw = 1 - _poisson_cdf(ltc, 8)
            resultado["corners_over85"] = round(_platt(p_corners85_raw, *PLATT["corners_over85"]) * 100, 1)

    return resultado


# ---------------------------------------------------------------------------
# PASO 2: cuotas -- de-vig y consenso
# ---------------------------------------------------------------------------

def obtener_cuotas_partido(conn, fixture_id) -> dict:
    """Devuelve, para un fixture, {mercado: {valor: [cuotas de cada casa]}}."""
    cur = conn.cursor()
    cur.execute("SELECT markets FROM cuotas WHERE fixture_id::text = %s", (str(fixture_id),))
    filas = cur.fetchall()

    cuotas = {}
    for (markets_json,) in filas:
        import json
        markets = markets_json if isinstance(markets_json, list) else json.loads(markets_json)
        for bet in markets:
            nombre = bet.get("name")
            for val in bet.get("values", []):
                valor = val.get("value")
                odd = val.get("odd")
                if odd is None:
                    continue
                try:
                    odd = float(odd)
                except (TypeError, ValueError):
                    continue
                cuotas.setdefault(nombre, {}).setdefault(valor, []).append(odd)
    return cuotas


def consenso_y_mejor(cuotas: dict, mercado: str, valor: str, opuesto: str):
    """Devuelve (probabilidad_consenso_sin_vig, mejor_cuota) para un
    mercado/valor concreto, o (None, None) si no hay datos suficientes."""
    lado = cuotas.get(mercado, {}).get(valor, [])
    contra = cuotas.get(mercado, {}).get(opuesto, [])
    if not lado or not contra:
        return None, None

    mejor_cuota = max(lado)

    # Emparejar de forma simple: usamos la media de cada lado por casa no
    # es trivial sin saber que cuota es de que casa aqui, asi que hacemos
    # una aproximacion razonable: probabilidad implicita media de cada
    # lado, normalizada entre si (quita la mayor parte del vig).
    prob_lado = sum(1 / o for o in lado) / len(lado)
    prob_contra = sum(1 / o for o in contra) / len(contra)
    prob_consenso = (prob_lado / (prob_lado + prob_contra)) * 100
    return round(prob_consenso, 1), mejor_cuota


# ---------------------------------------------------------------------------
# MODO EN VIVO
# ---------------------------------------------------------------------------

def modo_en_vivo(conn):
    print("Calculando fuerzas de ataque/defensa con todo el historico disponible...")
    fuerzas = calcular_fuerzas(conn)

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT p.api_fixture_id, p.liga_id, p.equipo_local, p.equipo_visitante
        FROM partidos p
        JOIN cuotas c ON c.fixture_id::text = p.api_fixture_id::text
        WHERE p.goles_local IS NULL
    """)
    # Nota: si tu tabla partidos no tiene partidos futuros (solo guarda
    # resultados ya jugados), esta consulta no encontrara nada -- en ese
    # caso el partido con cuotas pero sin jugar aun no estara en
    # "partidos" todavia. Se deja preparado para cuando la ingesta
    # incluya tambien los proximos partidos.
    fixtures = cur.fetchall()

    if not fixtures:
        print("No hay partidos con cuotas guardadas y sin resultado todavia. Nada que analizar.")
        return

    print(f"Analizando {len(fixtures)} partidos con cuotas guardadas...\n")
    encontrados = []

    for row in fixtures:
        probs = predecir_partido(fuerzas, row["liga_id"], row["equipo_local"], row["equipo_visitante"])
        if not probs:
            continue
        cuotas = obtener_cuotas_partido(conn, row["api_fixture_id"])
        for cond_id, prob in probs.items():
            mercado, valor, opuesto = MERCADOS[cond_id]
            prob_consenso, mejor_cuota = consenso_y_mejor(cuotas, mercado, valor, opuesto)
            if prob_consenso is None:
                continue
            edge = prob - prob_consenso
            if edge > MARGEN_SEGURIDAD_PP:
                encontrados.append({
                    "partido": f"{row['equipo_local']} vs {row['equipo_visitante']}",
                    "mercado": f"{mercado} ({valor})",
                    "nuestra_prob": prob,
                    "consenso": prob_consenso,
                    "edge": round(edge, 1),
                    "mejor_cuota": mejor_cuota,
                })

    if not encontrados:
        print("No se ha detectado ningun pick con valor (edge > "
              f"{MARGEN_SEGURIDAD_PP} puntos) en este momento.")
        return

    encontrados.sort(key=lambda x: x["edge"], reverse=True)
    print(f"{'='*70}\n{len(encontrados)} PICKS DE VALOR DETECTADOS\n{'='*70}")
    for p in encontrados:
        print(f"\n{p['partido']}")
        print(f"  Mercado: {p['mercado']}")
        print(f"  Nuestra probabilidad: {p['nuestra_prob']}%  |  Consenso mercado: {p['consenso']}%")
        print(f"  Ventaja: +{p['edge']} puntos  |  Mejor cuota disponible: {p['mejor_cuota']}")


# ---------------------------------------------------------------------------
# MODO BACKTEST
# ---------------------------------------------------------------------------

def modo_backtest(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT p.api_fixture_id, p.liga_id, p.equipo_local, p.equipo_visitante,
               p.goles_local, p.goles_visitante, p.corners_local, p.corners_visitante, p.fecha
        FROM partidos p
        JOIN cuotas c ON c.fixture_id::text = p.api_fixture_id::text
        WHERE p.goles_local IS NOT NULL
        ORDER BY p.fecha
    """)
    partidos = cur.fetchall()

    if not partidos:
        print("No hay partidos con resultado Y cuotas guardadas todavia. Nada que hacer backtest.")
        return

    fecha_corte = min(p["fecha"] for p in partidos)
    print(f"Backtest: {len(partidos)} partidos con resultado y cuotas "
          f"(desde {fecha_corte.date()}).")
    print("Calculando fuerzas SOLO con partidos anteriores a esa fecha (sin trampas)...")
    fuerzas = calcular_fuerzas(conn, fecha_corte=fecha_corte.isoformat())

    con_valor = []
    sin_valor = []

    for row in partidos:
        probs = predecir_partido(fuerzas, row["liga_id"], row["equipo_local"], row["equipo_visitante"])
        if not probs:
            continue
        cuotas = obtener_cuotas_partido(conn, row["api_fixture_id"])

        resultados_reales = {
            "btts": (row["goles_local"] or 0) > 0 and (row["goles_visitante"] or 0) > 0,
            "over25": (row["goles_local"] or 0) + (row["goles_visitante"] or 0) > 2,
            "home_goals": (row["goles_local"] or 0) > 1.5,
            "away_goals": (row["goles_visitante"] or 0) > 1.5,
            "corners_over85": (row["corners_local"] is not None and row["corners_visitante"] is not None
                                and (row["corners_local"] + row["corners_visitante"]) > 8.5),
        }

        for cond_id, prob in probs.items():
            mercado, valor, opuesto = MERCADOS[cond_id]
            prob_consenso, mejor_cuota = consenso_y_mejor(cuotas, mercado, valor, opuesto)
            if prob_consenso is None:
                continue
            edge = prob - prob_consenso
            acierto = resultados_reales[cond_id]
            entrada = {"acierto": acierto, "mejor_cuota": mejor_cuota, "edge": edge}
            (con_valor if edge > MARGEN_SEGURIDAD_PP else sin_valor).append(entrada)

    def resumen(nombre, lista):
        if not lista:
            print(f"\n{nombre}: sin datos.")
            return
        n = len(lista)
        aciertos = sum(1 for x in lista if x["acierto"])
        beneficio = sum((x["mejor_cuota"] - 1) if x["acierto"] else -1 for x in lista)
        roi = 100 * beneficio / n
        print(f"\n{nombre}:")
        print(f"  Apuestas: {n}  |  Aciertos: {aciertos} ({100*aciertos/n:.1f}%)")
        print(f"  Beneficio: {beneficio:+.2f} unidades  |  ROI: {roi:+.1f}%")

    print(f"\n{'='*70}\nRESULTADO DEL BACKTEST\n{'='*70}")
    resumen(f"Con valor (edge > {MARGEN_SEGURIDAD_PP} puntos)", con_valor)
    resumen("Sin valor (resto)", sin_valor)

    if len(con_valor) < 20:
        print(f"\nAVISO: solo {len(con_valor)} apuestas en el grupo 'con valor'. "
              "Con una muestra tan pequena, el resultado NO es estadisticamente "
              "fiable todavia -- repite este backtest cuando se acumulen mas dias "
              "de cuotas guardadas.")


def main():
    global MARGEN_SEGURIDAD_PP
    parser = argparse.ArgumentParser(description="Deteccion de picks de valor y backtesting.")
    parser.add_argument("--backtest", action="store_true",
                         help="Modo backtest: mide el rendimiento historico en vez de buscar picks nuevos.")
    parser.add_argument("--margen", type=float, default=None,
                         help=f"Margen de seguridad en puntos porcentuales (por defecto {MARGEN_SEGURIDAD_PP}).")
    args = parser.parse_args()

    if args.margen is not None:
        MARGEN_SEGURIDAD_PP = args.margen

    conn = get_conn()
    try:
        if args.backtest:
            modo_backtest(conn)
        else:
            modo_en_vivo(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
