"""
build_database_historical.py

Descarga temporadas HISTORICAS (pasadas) de las ligas que ya tenemos, para
tener muchos mas partidos con los que calcular fuerzas de equipo estables
y poder validar de verdad si el modelo generaliza (entrenar con temporadas
pasadas, comprobar contra una temporada completa nunca vista).

Es el mismo tipo de script que build_database.py, con dos diferencias clave
pensadas para no agotar tu cuota diaria de la API (7.500 peticiones/dia):

1. SALTA los partidos que ya existen en la tabla "partidos" -- no vuelve a
   gastar una peticion en pedir estadisticas de un partido historico que
   ya tienes guardado (los resultados de un partido de 2023 no cambian).

2. Tiene un PRESUPUESTO de peticiones por ejecucion (MAX_REQUESTS mas abajo,
   o el argumento --max-requests). En cuanto lo alcanza, para limpiamente
   y te dice exactamente por donde se quedo. La siguiente vez que lo
   ejecutes (otro dia, con la cuota ya renovada) sigue justo donde lo dejo,
   sin repetir nada, porque el paso 1 ya evita los duplicados.

COMO SE USA:
    python build_database_historical.py --max-requests 3000

Repite ese comando una vez al dia (o cuando tengas cuota libre) hasta que
te diga "Todo descargado, no queda nada pendiente." Necesitas las mismas
2 variables de entorno que build_database.py:
    DATABASE_URL
    API_FOOTBALL_KEY
"""

import os
import time
import sys
import argparse
import requests
import psycopg2

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

# Mismas 10 ligas que build_database.py. Se incluye Urvalsdeild (Islandia)
# por consistencia con el resto del sistema (para goles), aunque ya sabemos
# que esa liga nunca ha tenido datos de corners/tarjetas en ninguna
# temporada -- guardara 0 en esos campos, igual que ya pasa con la
# temporada actual, y se sigue excluyendo aparte en el analisis.
LIGAS = [
    {"api_league_id": 113, "nombre": "Allsvenskan", "pais": "Suecia"},
    {"api_league_id": 103, "nombre": "Eliteserien", "pais": "Noruega"},
    {"api_league_id": 119, "nombre": "Superliga", "pais": "Dinamarca"},
    {"api_league_id": 164, "nombre": "Urvalsdeild", "pais": "Islandia"},
    {"api_league_id": 244, "nombre": "Veikkausliiga", "pais": "Finlandia"},
    {"api_league_id": 283, "nombre": "Liga I", "pais": "Rumania"},
    {"api_league_id": 169, "nombre": "Super League", "pais": "China"},
    {"api_league_id": 98, "nombre": "J1 League", "pais": "Japon"},
    {"api_league_id": 288, "nombre": "Premier Soccer League", "pais": "Sudafrica"},
    {"api_league_id": 253, "nombre": "Major League Soccer", "pais": "EEUU"},
]

# Temporadas historicas a descargar (la actual, 2026, ya la gestiona
# build_database.py / el scheduler diario -- este script es solo para
# rellenar el pasado).
TEMPORADAS = [2023, 2024, 2025]

API_BASE_URL = "https://v3.football.api-sports.io"

# Segundos entre llamadas a la API para no disparar el limite por minuto.
PAUSA_ENTRE_LLAMADAS = 1.0

# Presupuesto de peticiones por defecto si no se pasa --max-requests.
# Deja margen de sobra para que el bot en produccion (analisis diario +
# cuotas) siga funcionando sin problemas el mismo dia.
MAX_REQUESTS_POR_DEFECTO = 3000


# ---------------------------------------------------------------------------
# CONEXION A SUPABASE
# ---------------------------------------------------------------------------

def conectar_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: no encuentro la variable de entorno DATABASE_URL.")
        sys.exit(1)
    return psycopg2.connect(database_url)


def crear_tablas_si_no_existen(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ligas (
                id SERIAL PRIMARY KEY,
                api_league_id INT UNIQUE NOT NULL,
                nombre TEXT NOT NULL,
                pais TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS partidos (
                id SERIAL PRIMARY KEY,
                api_fixture_id INT UNIQUE NOT NULL,
                liga_id INT REFERENCES ligas(id),
                fecha TIMESTAMP,
                equipo_local TEXT,
                equipo_visitante TEXT,
                goles_local INT,
                goles_visitante INT,
                corners_local INT,
                corners_visitante INT,
                tarjetas_amarillas_local INT,
                tarjetas_amarillas_visitante INT,
                tarjetas_rojas_local INT,
                tarjetas_rojas_visitante INT,
                tiros_totales_local INT,
                tiros_totales_visitante INT,
                tiros_puerta_local INT,
                tiros_puerta_visitante INT
            );
        """)
    conn.commit()


def guardar_liga_si_no_existe(conn, liga):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ligas (api_league_id, nombre, pais)
            VALUES (%s, %s, %s)
            ON CONFLICT (api_league_id) DO NOTHING
            RETURNING id;
        """, (liga["api_league_id"], liga["nombre"], liga["pais"]))
        fila = cur.fetchone()
        if fila is None:
            cur.execute("SELECT id FROM ligas WHERE api_league_id = %s", (liga["api_league_id"],))
            fila = cur.fetchone()
    conn.commit()
    return fila[0]


def fixture_ids_ya_guardados(conn, liga_id):
    """Devuelve el conjunto de api_fixture_id que YA estan en la tabla
    partidos para esta liga, para poder saltarlos sin gastar peticion."""
    with conn.cursor() as cur:
        cur.execute("SELECT api_fixture_id FROM partidos WHERE liga_id = %s", (liga_id,))
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# LLAMADAS A LA API-FOOTBALL
# ---------------------------------------------------------------------------

def cabeceras_api():
    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        print("ERROR: no encuentro la variable de entorno API_FOOTBALL_KEY.")
        sys.exit(1)
    return {"x-apisports-key": api_key}


def obtener_partidos_finalizados(league_id, season):
    url = f"{API_BASE_URL}/fixtures"
    params = {"league": league_id, "season": season, "status": "FT"}
    resp = requests.get(url, headers=cabeceras_api(), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", [])


def obtener_estadisticas_partido(fixture_id):
    url = f"{API_BASE_URL}/fixtures/statistics"
    params = {"fixture": fixture_id}
    resp = requests.get(url, headers=cabeceras_api(), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", [])


def extraer_valor_stat(stats_equipo, nombre_stat):
    for item in stats_equipo.get("statistics", []):
        if item.get("type") == nombre_stat:
            valor = item.get("value")
            if valor is None:
                return 0
            if isinstance(valor, str) and valor.endswith("%"):
                return int(valor.replace("%", ""))
            return int(valor)
    return 0


# ---------------------------------------------------------------------------
# GUARDAR UN PARTIDO
# ---------------------------------------------------------------------------

def guardar_partido(conn, liga_id, fixture, stats_response):
    fixture_id = fixture["fixture"]["id"]
    fecha = fixture["fixture"]["date"]
    equipo_local = fixture["teams"]["home"]["name"]
    equipo_visitante = fixture["teams"]["away"]["name"]
    goles_local = fixture["goals"]["home"]
    goles_visitante = fixture["goals"]["away"]

    corners_local = corners_visitante = 0
    tarjetas_am_local = tarjetas_am_visitante = 0
    tarjetas_rj_local = tarjetas_rj_visitante = 0
    tiros_tot_local = tiros_tot_visitante = 0
    tiros_puerta_local = tiros_puerta_visitante = 0

    if len(stats_response) == 2:
        stats_local = stats_response[0]
        stats_visitante = stats_response[1]

        corners_local = extraer_valor_stat(stats_local, "Corner Kicks")
        corners_visitante = extraer_valor_stat(stats_visitante, "Corner Kicks")

        tarjetas_am_local = extraer_valor_stat(stats_local, "Yellow Cards")
        tarjetas_am_visitante = extraer_valor_stat(stats_visitante, "Yellow Cards")

        tarjetas_rj_local = extraer_valor_stat(stats_local, "Red Cards")
        tarjetas_rj_visitante = extraer_valor_stat(stats_visitante, "Red Cards")

        tiros_tot_local = extraer_valor_stat(stats_local, "Total Shots")
        tiros_tot_visitante = extraer_valor_stat(stats_visitante, "Total Shots")

        tiros_puerta_local = extraer_valor_stat(stats_local, "Shots on Goal")
        tiros_puerta_visitante = extraer_valor_stat(stats_visitante, "Shots on Goal")

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO partidos (
                api_fixture_id, liga_id, fecha, equipo_local, equipo_visitante,
                goles_local, goles_visitante,
                corners_local, corners_visitante,
                tarjetas_amarillas_local, tarjetas_amarillas_visitante,
                tarjetas_rojas_local, tarjetas_rojas_visitante,
                tiros_totales_local, tiros_totales_visitante,
                tiros_puerta_local, tiros_puerta_visitante
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s
            )
            ON CONFLICT (api_fixture_id) DO UPDATE SET
                goles_local = EXCLUDED.goles_local,
                goles_visitante = EXCLUDED.goles_visitante,
                corners_local = EXCLUDED.corners_local,
                corners_visitante = EXCLUDED.corners_visitante,
                tarjetas_amarillas_local = EXCLUDED.tarjetas_amarillas_local,
                tarjetas_amarillas_visitante = EXCLUDED.tarjetas_amarillas_visitante,
                tarjetas_rojas_local = EXCLUDED.tarjetas_rojas_local,
                tarjetas_rojas_visitante = EXCLUDED.tarjetas_rojas_visitante,
                tiros_totales_local = EXCLUDED.tiros_totales_local,
                tiros_totales_visitante = EXCLUDED.tiros_totales_visitante,
                tiros_puerta_local = EXCLUDED.tiros_puerta_local,
                tiros_puerta_visitante = EXCLUDED.tiros_puerta_visitante;
        """, (
            fixture_id, liga_id, fecha, equipo_local, equipo_visitante,
            goles_local, goles_visitante,
            corners_local, corners_visitante,
            tarjetas_am_local, tarjetas_am_visitante,
            tarjetas_rj_local, tarjetas_rj_visitante,
            tiros_tot_local, tiros_tot_visitante,
            tiros_puerta_local, tiros_puerta_visitante,
        ))
    conn.commit()


# ---------------------------------------------------------------------------
# PROGRAMA PRINCIPAL
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Descarga historica por tandas, sin pasarte de cuota.")
    parser.add_argument(
        "--max-requests", type=int, default=MAX_REQUESTS_POR_DEFECTO,
        help=f"Maximo de peticiones a la API en esta ejecucion (por defecto {MAX_REQUESTS_POR_DEFECTO})."
    )
    args = parser.parse_args()
    presupuesto = args.max_requests

    print(f"Presupuesto de esta ejecucion: {presupuesto} peticiones a la API.")
    print("Conectando a Supabase...")
    conn = conectar_db()
    crear_tablas_si_no_existen(conn)

    peticiones_usadas = 0
    total_guardados = 0
    total_saltados = 0
    agotado = False

    for liga in LIGAS:
        if agotado:
            break
        liga_id = guardar_liga_si_no_existe(conn, liga)
        ya_guardados = fixture_ids_ya_guardados(conn, liga_id)

        for temporada in TEMPORADAS:
            if agotado:
                break
            print(f"\n--- {liga['nombre']} ({liga['pais']}) - temporada {temporada} ---")

            if peticiones_usadas >= presupuesto:
                agotado = True
                break

            print("Pidiendo lista de partidos finalizados...")
            partidos = obtener_partidos_finalizados(liga["api_league_id"], temporada)
            peticiones_usadas += 1
            print(f"Encontrados {len(partidos)} partidos finalizados en la API.")

            pendientes = [f for f in partidos if f["fixture"]["id"] not in ya_guardados]
            print(f"De esos, {len(pendientes)} no estan aun en tu base de datos.")

            for i, fixture in enumerate(pendientes, start=1):
                if peticiones_usadas >= presupuesto:
                    print(f"\n>>> Presupuesto de {presupuesto} peticiones alcanzado. "
                          f"Parando aqui limpiamente.")
                    agotado = True
                    break

                fixture_id = fixture["fixture"]["id"]
                local = fixture["teams"]["home"]["name"]
                visitante = fixture["teams"]["away"]["name"]
                print(f"  [{i}/{len(pendientes)}] {local} vs {visitante} (fixture {fixture_id})")

                stats = obtener_estadisticas_partido(fixture_id)
                peticiones_usadas += 1
                guardar_partido(conn, liga_id, fixture, stats)
                ya_guardados.add(fixture_id)
                total_guardados += 1

                time.sleep(PAUSA_ENTRE_LLAMADAS)

            total_saltados += len(partidos) - len(pendientes)

    conn.close()

    print(f"\n{'='*60}")
    print(f"Peticiones usadas esta ejecucion: {peticiones_usadas}")
    print(f"Partidos nuevos guardados: {total_guardados}")
    print(f"Partidos que ya tenias (saltados, sin gastar peticion): {total_saltados}")
    if agotado:
        print("\nQuedan partidos pendientes por descargar.")
        print("Vuelve a ejecutar este mismo comando manana (o cuando tengas cuota libre)")
        print("y seguira justo donde lo dejaste, sin repetir nada.")
    else:
        print("\nTodo descargado, no queda nada pendiente para las temporadas configuradas.")


if __name__ == "__main__":
    main()
