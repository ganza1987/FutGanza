"""
value_bets.py

Automatiza lo que hasta ahora haciamos a mano en el editor SQL de Supabase:

1. Calcula la probabilidad de cada partido para 4 mercados de goles ya
   validados (BTTS, Over 2.5 goles, Local +1.5, Visitante +1.5) usando el
   modelo Poisson + calibracion de Platt validado con split temporal
   riguroso, MAS corners -- pero para corners no se limita a la linea 8.5:
   calcula la probabilidad para CUALQUIER linea que ofrezca cada casa
   (8.5, 9.5, 10.5...), usando una correccion de Platt propia por linea
   (interpolada entre las lineas validadas), ya que reutilizar la
   correccion de 8.5 en otras lineas se demostro que rompe la calibracion.
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
}

# Correccion de Platt para el modelo COMBINADO de BTTS (media de la lambda
# de goles y la lambda "traducida" de tiros a puerta). Validado con split
# temporal riguroso: reduce el error medio de ~2.8pp (solo goles) a
# ~1.8pp, y a diferencia del modelo de solo tiros a puerta, SI mantiene
# capacidad de diferenciar partidos (rango de 12.7pp entre deciles, frente
# a solo 1.1pp con tiros a puerta en solitario). Solo se ha validado para
# BTTS -- los otros mercados de goles (over25, home_goals, away_goals)
# siguen usando el modelo de solo-goles de siempre.
PLATT_BTTS_COMBINADO = (0.4246, 0.1867)

# Correccion de Platt POR LINEA de corners (no solo 8.5). Cada casa de
# apuestas ofrece una linea distinta segun el partido, y validamos que la
# correccion de 8.5 NO se puede reutilizar sin mas en otras lineas (el
# error se dispara cuanto mas lejos de 8.5 esta la linea) -- pero SI se
# puede calcular una correccion propia por linea, porque a diferencia de
# partir los datos por liga (donde la muestra se reduce mucho), aqui
# TODOS los partidos de calibracion sirven para cualquier linea (es el
# mismo modelo de fuerzas, solo cambia donde se corta la Poisson).
# Validado con split temporal riguroso en las lineas 9.5 y 11.5 (mejora de
# ~8pp de error a ~2-4pp). Para lineas intermedias se interpola
# linealmente entre los dos puntos mas cercanos; fuera del rango 7.5-11.5
# se usa el coeficiente del extremo mas cercano (extrapolar mas alla no
# esta validado).
CORNERS_PLATT_PUNTOS = [
    (7.5, 0.2885, 0.6670),
    (8.5, 0.2953, 0.3493),
    (9.5, 0.3098, 0.0028),
    (10.5, 0.3400, -0.2797),
    (11.5, 0.3623, -0.5418),
]


def _platt_corners(linea: float) -> tuple[float, float]:
    """Interpola (o extrapola, acotando a los extremos) la correccion de
    Platt para una linea de corners cualquiera, a partir de la tabla
    validada en CORNERS_PLATT_PUNTOS."""
    puntos = CORNERS_PLATT_PUNTOS
    if linea <= puntos[0][0]:
        return puntos[0][1], puntos[0][2]
    if linea >= puntos[-1][0]:
        return puntos[-1][1], puntos[-1][2]
    for (x0, s0, i0), (x1, s1, i1) in zip(puntos, puntos[1:]):
        if x0 <= linea <= x1:
            t = (linea - x0) / (x1 - x0)
            return s0 + t * (s1 - s0), i0 + t * (i1 - i0)
    return puntos[-1][1], puntos[-1][2]  # nunca deberia llegar aqui

# Mapeo condicion -> (nombre de mercado en la cuota, valor a buscar, valor opuesto)
# Corners NO va aqui -- se trata aparte porque la linea (8.5, 9.5, 10.5...)
# cambia segun la casa y el partido. Ver extraer_lineas_corners() y
# predecir_corners_linea() mas abajo.
MERCADOS = {
    "btts": ("Both Teams Score", "Yes", "No"),
    "over25": ("Goals Over/Under", "Over 2.5", "Under 2.5"),
    "home_goals": ("Total - Home", "Over 1.5", "Under 1.5"),
    "away_goals": ("Total - Away", "Over 1.5", "Under 1.5"),
}

MERCADO_CORNERS = "Corners Over Under"


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

    # --- Tiros a puerta (excluye filas 0-0 contaminadas). Se usan SOLO
    # para BTTS: combinado con el modelo de goles, reduce el ruido de la
    # "suerte" en el resultado (un tiro que da en el palo y entra, etc.)
    # sin perder la capacidad de diferenciar partidos que tiene el modelo
    # de goles solo. Validado con split temporal riguroso -- SOLO para
    # BTTS, no para los otros mercados de goles (no se ha comprobado ahi).
    where_shots = where_fecha + (" AND" if where_fecha else "WHERE") + \
        " NOT (tiros_puerta_local=0 AND tiros_puerta_visitante=0)"
    cur.execute(f"""
        SELECT liga_id, avg(tiros_puerta_local) as avg_home, avg(tiros_puerta_visitante) as avg_away
        FROM partidos {where_shots} GROUP BY liga_id
    """, params)
    liga_avg_shots = {r["liga_id"]: (float(r["avg_home"]), float(r["avg_away"])) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT liga_id, equipo_local as team, avg(tiros_puerta_local) as gf, avg(tiros_puerta_visitante) as ga
        FROM partidos {where_shots} GROUP BY liga_id, equipo_local
    """, params)
    team_home_shots = {(r["liga_id"], r["team"]): (float(r["gf"]), float(r["ga"])) for r in cur.fetchall()}

    cur.execute(f"""
        SELECT liga_id, equipo_visitante as team, avg(tiros_puerta_visitante) as gf, avg(tiros_puerta_local) as ga
        FROM partidos {where_shots} GROUP BY liga_id, equipo_visitante
    """, params)
    team_away_shots = {(r["liga_id"], r["team"]): (float(r["gf"]), float(r["ga"])) for r in cur.fetchall()}

    return {
        "liga_avg_goles": liga_avg_goles,
        "team_home_goles": team_home_goles,
        "team_away_goles": team_away_goles,
        "liga_avg_corners": liga_avg_corners,
        "team_home_corners": team_home_corners,
        "team_away_corners": team_away_corners,
        "liga_avg_shots": liga_avg_shots,
        "team_home_shots": team_home_shots,
        "team_away_shots": team_away_shots,
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

    lt = lh + lav
    p_over25_raw = 1 - math.exp(-lt) * (1 + lt + (lt ** 2) / 2)
    p_home15_raw = 1 - math.exp(-lh) * (1 + lh)
    p_away15_raw = 1 - math.exp(-lav) * (1 + lav)

    resultado = {
        "over25": round(_platt(p_over25_raw, *PLATT["over25"]) * 100, 1),
        "home_goals": round(_platt(p_home15_raw, *PLATT["home_goals"]) * 100, 1),
        "away_goals": round(_platt(p_away15_raw, *PLATT["away_goals"]) * 100, 1),
    }

    # BTTS: modelo combinado (media de lambda de goles y lambda de tiros a
    # puerta "traducida" a goles via tasa de conversion de la liga), si
    # hay datos de tiros a puerta disponibles; si no, cae al modelo de
    # solo goles (con su propia calibracion) para no dejar el mercado sin
    # cubrir.
    la_s = fuerzas["liga_avg_shots"].get(liga_id)
    th_s = fuerzas["team_home_shots"].get((liga_id, home))
    ta_s = fuerzas["team_away_shots"].get((liga_id, away))
    if la_s and th_s and ta_s:
        avg_home_s, avg_away_s = la_s
        attack_home_s = th_s[0] / avg_home_s
        defense_away_s = ta_s[1] / avg_home_s
        attack_away_s = ta_s[0] / avg_away_s
        defense_home_s = th_s[1] / avg_away_s
        conv_home = avg_home_g / avg_home_s
        conv_away = avg_away_g / avg_away_s
        lh_s = avg_home_s * attack_home_s * defense_away_s * conv_home
        lav_s = avg_away_s * attack_away_s * defense_home_s * conv_away

        lh_comb = (lh + lh_s) / 2
        lav_comb = (lav + lav_s) / 2
        p_btts_raw = (1 - math.exp(-lh_comb)) * (1 - math.exp(-lav_comb))
        resultado["btts"] = round(_platt(p_btts_raw, *PLATT_BTTS_COMBINADO) * 100, 1)
    else:
        p_btts_raw = (1 - math.exp(-lh)) * (1 - math.exp(-lav))
        resultado["btts"] = round(_platt(p_btts_raw, *PLATT["btts"]) * 100, 1)

    return resultado


def lambda_corners_total(fuerzas: dict, liga_id: int, home: str, away: str) -> float | None:
    """Devuelve el numero esperado de corners totales (lambda) para un
    partido, o None si falta algun dato o la liga no tiene datos reales de
    corners (ver LIGA_ID_SIN_CORNERS). Se reutiliza tanto para la linea
    8.5 como para cualquier otra linea que ofrezca la casa."""
    if liga_id == LIGA_ID_SIN_CORNERS:
        return None
    la_c = fuerzas["liga_avg_corners"].get(liga_id)
    th_c = fuerzas["team_home_corners"].get((liga_id, home))
    ta_c = fuerzas["team_away_corners"].get((liga_id, away))
    if not (la_c and th_c and ta_c):
        return None
    avg_home_c, avg_away_c = la_c
    attack_home_c = th_c[0] / avg_home_c
    defense_away_c = ta_c[1] / avg_home_c
    attack_away_c = ta_c[0] / avg_away_c
    defense_home_c = th_c[1] / avg_away_c
    lhc = avg_home_c * attack_home_c * defense_away_c
    lavc = avg_away_c * attack_away_c * defense_home_c
    return lhc + lavc


def predecir_corners_linea(fuerzas: dict, liga_id: int, home: str, away: str, linea: float) -> float | None:
    """Probabilidad calibrada de que los corners totales superen "linea"
    (por ejemplo 8.5, 9.5, 10.5...), usando la correccion de Platt
    especifica de esa linea (interpolada si hace falta). Devuelve None si
    no hay datos suficientes."""
    ltc = lambda_corners_total(fuerzas, liga_id, home, away)
    if ltc is None:
        return None
    p_raw = 1 - _poisson_cdf(ltc, math.floor(linea))
    slope, intercept = _platt_corners(linea)
    return round(_platt(p_raw, slope, intercept) * 100, 1)


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


def extraer_lineas_corners(cuotas: dict) -> list[float]:
    """Devuelve la lista de lineas de corners (8.5, 9.5, 10.5...) para las
    que hay tanto el lado "Over X" como "Under X" en el mercado "Corners
    Over Under" de este partido, RESTRINGIDO a las 5 lineas EXACTAS que
    hemos validado (ver CORNERS_PLATT_PUNTOS: 7.5, 8.5, 9.5, 10.5, 11.5).

    Las casas ofrecen decenas de lineas por partido -- no solo enteras y
    en .5, tambien en cuartos (7.75, 8.25, 9.75...) y numeros enteros (8,
    9, 10, 11). Solo hemos ajustado y comprobado la correccion de Platt en
    los 5 puntos exactos de la tabla; interpolar tambien para esas lineas
    intermedias (que no son nada raras, aparecen constantemente) demostro
    dar resultados malos en el backtest -- ROI negativo y muy irregular en
    TODAS las lineas no validadas. Asi que aqui se exige coincidencia
    exacta (con un margen minimo por redondeos de coma flotante), no un
    rango."""
    import re
    lineas_validas = {p[0] for p in CORNERS_PLATT_PUNTOS}
    mercado = cuotas.get(MERCADO_CORNERS, {})
    lineas_over = set()
    lineas_under = set()
    for valor in mercado:
        m = re.match(r"^(Over|Under)\s+([\d.]+)$", valor.strip())
        if not m:
            continue
        numero = float(m.group(2))
        if not any(abs(numero - lv) < 0.01 for lv in lineas_validas):
            continue
        if m.group(1) == "Over":
            lineas_over.add(numero)
        else:
            lineas_under.add(numero)
    return sorted(lineas_over & lineas_under)


def consenso_y_mejor_corners(cuotas: dict, linea: float):
    """Igual que consenso_y_mejor(), pero para una linea de corners
    concreta dentro del mercado 'Corners Over Under'."""
    valor_over = f"Over {linea:g}"
    valor_under = f"Under {linea:g}"
    return consenso_y_mejor(cuotas, MERCADO_CORNERS, valor_over, valor_under)



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
# Picks detectados en vivo: guardarlos y verificarlos mas tarde
# ---------------------------------------------------------------------------
# Tabla nueva, separada de todo lo demas (no toca picks_historial del bot
# de Telegram ni ninguna tabla existente). Sirve para que "modo en vivo"
# deje registro de cada pick que detecta, y poder comprobar mas adelante
# -- una vez el partido se haya jugado -- si acerto o no.

def crear_tabla_value_picks(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS value_picks_historial (
            id SERIAL PRIMARY KEY,
            fixture_id INT NOT NULL,
            condicion TEXT NOT NULL,
            partido TEXT,
            mercado TEXT,
            nuestra_prob NUMERIC,
            consenso NUMERIC,
            edge NUMERIC,
            mejor_cuota NUMERIC,
            fecha_deteccion TIMESTAMP DEFAULT NOW(),
            resultado TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()


def guardar_picks_detectados(conn, encontrados: list[dict]):
    """Inserta cada pick detectado en value_picks_historial, con
    resultado='pending' hasta que verificar_picks_pendientes() los
    resuelva mas adelante."""
    crear_tabla_value_picks(conn)
    cur = conn.cursor()
    for p in encontrados:
        cur.execute("""
            INSERT INTO value_picks_historial
                (fixture_id, condicion, partido, mercado, nuestra_prob, consenso, edge, mejor_cuota)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (p["fixture_id"], p["condicion"], p["partido"], p["mercado"],
              p["nuestra_prob"], p["consenso"], p["edge"], p["mejor_cuota"]))
    conn.commit()


def _acierto_condicion(condicion: str, goles_local, goles_visitante, corners_local, corners_visitante):
    """Misma logica que resultados_reales/corners_reales en modo_backtest,
    reutilizada aqui para no duplicar (y no desincronizar) el criterio de
    acierto entre el backtest y la verificacion de picks en vivo."""
    if condicion == "btts":
        return (goles_local or 0) > 0 and (goles_visitante or 0) > 0
    if condicion == "over25":
        return (goles_local or 0) + (goles_visitante or 0) > 2
    if condicion == "home_goals":
        return (goles_local or 0) > 1.5
    if condicion == "away_goals":
        return (goles_visitante or 0) > 1.5
    if condicion.startswith("corners_"):
        if corners_local is None or corners_visitante is None:
            return None
        linea = float(condicion.split("_", 1)[1])
        return (corners_local + corners_visitante) > linea
    return None


def verificar_picks_pendientes(conn):
    """Recorre los picks guardados con resultado='pending', comprueba
    contra "partidos" si ya tienen resultado final, y actualiza a 'hit' o
    'miss' segun corresponda. Devuelve (verificados, siguen_pendientes)."""
    crear_tabla_value_picks(conn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM value_picks_historial WHERE resultado = 'pending'")
    pendientes = cur.fetchall()

    if not pendientes:
        return 0, 0

    verificados = 0
    cur2 = conn.cursor()
    for p in pendientes:
        cur.execute(
            "SELECT goles_local, goles_visitante, corners_local, corners_visitante "
            "FROM partidos WHERE api_fixture_id = %s",
            (p["fixture_id"],)
        )
        partido = cur.fetchone()
        if not partido or partido["goles_local"] is None:
            continue  # el partido todavia no se ha jugado (o no esta en partidos)

        acierto = _acierto_condicion(
            p["condicion"], partido["goles_local"], partido["goles_visitante"],
            partido["corners_local"], partido["corners_visitante"]
        )
        if acierto is None:
            continue

        resultado = "hit" if acierto else "miss"
        cur2.execute(
            "UPDATE value_picks_historial SET resultado = %s WHERE id = %s",
            (resultado, p["id"])
        )
        verificados += 1

    conn.commit()
    return verificados, len(pendientes) - verificados


# ---------------------------------------------------------------------------
# MODO EN VIVO
# ---------------------------------------------------------------------------

def modo_en_vivo(conn):
    verificados, siguen_pendientes = verificar_picks_pendientes(conn)
    if verificados:
        print(f"Verificados {verificados} picks de rondas anteriores (ya se jugaron).")
    if siguen_pendientes:
        print(f"{siguen_pendientes} picks siguen pendientes (su partido aun no se ha jugado).")

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
                    "fixture_id": row["api_fixture_id"],
                    "condicion": cond_id,
                    "partido": f"{row['equipo_local']} vs {row['equipo_visitante']}",
                    "mercado": f"{mercado} ({valor})",
                    "nuestra_prob": prob,
                    "consenso": prob_consenso,
                    "edge": round(edge, 1),
                    "mejor_cuota": mejor_cuota,
                })

        # Corners: cualquier linea que ofrezca la casa, no solo 8.5
        for linea in extraer_lineas_corners(cuotas):
            prob_c = predecir_corners_linea(fuerzas, row["liga_id"], row["equipo_local"], row["equipo_visitante"], linea)
            if prob_c is None:
                continue
            prob_consenso, mejor_cuota = consenso_y_mejor_corners(cuotas, linea)
            if prob_consenso is None:
                continue
            edge = prob_c - prob_consenso
            if edge > MARGEN_SEGURIDAD_PP:
                encontrados.append({
                    "fixture_id": row["api_fixture_id"],
                    "condicion": f"corners_{linea:g}",
                    "partido": f"{row['equipo_local']} vs {row['equipo_visitante']}",
                    "mercado": f"Corners Over Under (Over {linea:g})",
                    "nuestra_prob": prob_c,
                    "consenso": prob_consenso,
                    "edge": round(edge, 1),
                    "mejor_cuota": mejor_cuota,
                })

    if not encontrados:
        print("No se ha detectado ningun pick con valor (edge > "
              f"{MARGEN_SEGURIDAD_PP} puntos) en este momento.")
        return

    guardar_picks_detectados(conn, encontrados)

    encontrados.sort(key=lambda x: x["edge"], reverse=True)
    print(f"{'='*70}\n{len(encontrados)} PICKS DE VALOR DETECTADOS (guardados para verificar mas tarde)\n{'='*70}")
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
        }

        for cond_id, prob in probs.items():
            mercado, valor, opuesto = MERCADOS[cond_id]
            prob_consenso, mejor_cuota = consenso_y_mejor(cuotas, mercado, valor, opuesto)
            if prob_consenso is None:
                continue
            edge = prob - prob_consenso
            acierto = resultados_reales[cond_id]
            entrada = {"acierto": acierto, "mejor_cuota": mejor_cuota, "edge": edge, "mercado": cond_id}
            (con_valor if edge > MARGEN_SEGURIDAD_PP else sin_valor).append(entrada)

        # Corners: cualquier linea que ofrezca la casa, no solo 8.5
        corners_reales = None
        if row["corners_local"] is not None and row["corners_visitante"] is not None:
            corners_reales = row["corners_local"] + row["corners_visitante"]
        for linea in extraer_lineas_corners(cuotas):
            if corners_reales is None:
                continue
            prob_c = predecir_corners_linea(fuerzas, row["liga_id"], row["equipo_local"], row["equipo_visitante"], linea)
            if prob_c is None:
                continue
            prob_consenso, mejor_cuota = consenso_y_mejor_corners(cuotas, linea)
            if prob_consenso is None:
                continue
            edge = prob_c - prob_consenso
            acierto = corners_reales > linea
            entrada = {"acierto": acierto, "mejor_cuota": mejor_cuota, "edge": edge, "mercado": f"corners_{linea:g}"}
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

    # Desglose por mercado -- para ver si el problema (o la ventaja) esta
    # concentrado en un mercado concreto en vez de repartido por igual.
    # Se calcula sobre TODAS las apuestas (con y sin valor juntas), porque
    # aqui interesa saber si el modelo en si acierta bien en cada mercado,
    # no solo las que pasaron el filtro de valor.
    print(f"\n{'='*70}\nDESGLOSE POR MERCADO (todas las apuestas, con y sin valor)\n{'='*70}")
    todas = con_valor + sin_valor
    por_mercado: dict[str, list] = {}
    for x in todas:
        por_mercado.setdefault(x["mercado"], []).append(x)
    for mercado in sorted(por_mercado):
        resumen(f"  {mercado}", por_mercado[mercado])

    if len(con_valor) < 20:
        print(f"\nAVISO: solo {len(con_valor)} apuestas en el grupo 'con valor'. "
              "Con una muestra tan pequena, el resultado NO es estadisticamente "
              "fiable todavia -- repite este backtest cuando se acumulen mas dias "
              "de cuotas guardadas.")


def resumen_picks_en_vivo(conn):
    """Muestra el rendimiento acumulado de los picks detectados en modo en
    vivo y ya guardados en value_picks_historial: cuantos han acertado,
    fallado, o siguen pendientes de que se juegue su partido."""
    crear_tabla_value_picks(conn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM value_picks_historial ORDER BY fecha_deteccion")
    filas = cur.fetchall()

    if not filas:
        print("Todavia no se ha guardado ningun pick en vivo. Ejecuta 'python value_bets.py' (sin flags) para empezar a detectarlos.")
        return

    pendientes = [f for f in filas if f["resultado"] == "pending"]
    resueltos = [f for f in filas if f["resultado"] in ("hit", "miss")]

    print(f"{'='*70}\nRESUMEN DE PICKS EN VIVO GUARDADOS\n{'='*70}")
    print(f"Total guardados: {len(filas)}  |  Pendientes: {len(pendientes)}  |  Resueltos: {len(resueltos)}")

    if resueltos:
        aciertos = sum(1 for f in resueltos if f["resultado"] == "hit")
        beneficio = sum((float(f["mejor_cuota"]) - 1) if f["resultado"] == "hit" else -1 for f in resueltos)
        n = len(resueltos)
        print(f"\nDe los resueltos: {aciertos} aciertos ({100*aciertos/n:.1f}%)  |  "
              f"Beneficio: {beneficio:+.2f} unidades  |  ROI: {100*beneficio/n:+.1f}%")
        if n < 20:
            print("\nAVISO: menos de 20 picks resueltos. Con una muestra tan pequena, "
                  "el resultado NO es estadisticamente fiable todavia.")
    else:
        print("\nAun no hay ningun pick resuelto (todos siguen pendientes de jugarse).")


def main():
    global MARGEN_SEGURIDAD_PP
    parser = argparse.ArgumentParser(description="Deteccion de picks de valor y backtesting.")
    parser.add_argument("--backtest", action="store_true",
                         help="Modo backtest: mide el rendimiento historico en vez de buscar picks nuevos.")
    parser.add_argument("--verificar", action="store_true",
                         help="Solo verifica los picks en vivo pendientes (ver si ya se jugaron), sin buscar nuevos ni hacer backtest.")
    parser.add_argument("--resumen", action="store_true",
                         help="Muestra el rendimiento acumulado de los picks en vivo ya guardados (aciertos, ROI), sin buscar nuevos.")
    parser.add_argument("--margen", type=float, default=None,
                         help=f"Margen de seguridad en puntos porcentuales (por defecto {MARGEN_SEGURIDAD_PP}).")
    args = parser.parse_args()

    if args.margen is not None:
        MARGEN_SEGURIDAD_PP = args.margen

    conn = get_conn()
    try:
        if args.verificar:
            verificados, siguen_pendientes = verificar_picks_pendientes(conn)
            print(f"Verificados {verificados} picks (se jugaron ya). Siguen pendientes: {siguen_pendientes}.")
        elif args.resumen:
            resumen_picks_en_vivo(conn)
        elif args.backtest:
            modo_backtest(conn)
        else:
            modo_en_vivo(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
