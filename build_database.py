"""
build_database.py

Este script hace 3 cosas, en orden:
1. Se conecta a tu base de datos en Supabase.
2. Se conecta a la API de API-Football.
3. Para cada liga que le digas (Suecia y Noruega), descarga los partidos
   ya jugados y sus estadisticas (corners, tarjetas, tiros, goles) y los
   guarda en la tabla "partidos" de Supabase.

Si vuelves a correr el script varias veces, no duplica partidos: actualiza
los que ya existen y anade solo los nuevos.

COMO SE USA (resumen, mas abajo esta la guia paso a paso completa):
    python build_database.py

Necesitas tener configuradas 2 variables de entorno antes de correrlo:
    DATABASE_URL       -> la cadena de conexion de Supabase (pooler, puerto 6543)
    API_FOOTBALL_KEY   -> tu clave de API-Football
"""

import os
import time
import sys
import requests
import psycopg2

# ---------------------------------------------------------------------------
# CONFIGURACION: aqui defines que ligas y que temporada quieres descargar
# ---------------------------------------------------------------------------

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

TEMPORADA = 2026  # temporada que quieres descargar

API_BASE_URL = "https://v3.football.api-sports.io"

# Cuantos segundos esperar entre llamadas a la API para no pasarte del limite
PAUSA_ENTRE_LLAMADAS = 1.0


# ---------------------------------------------------------------------------
# CONEXION A SUPABASE
# ---------------------------------------------------------------------------

def conectar_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: no encuentro la variable de entorno DATABASE_URL.")
        print("Revisa el paso 3 de la guia antes de continuar.")
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
    print("Tablas listas (creadas si no existian).")


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
            # ya existia, la buscamos
            cur.execute("SELECT id FROM ligas WHERE api_league_id = %s", (liga["api_league_id"],))
            fila = cur.fetchone()
    conn.commit()
    return fila[0]


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
    """Devuelve la lista de partidos ya jugados (status FT) de una liga/temporada."""
    url = f"{API_BASE_URL}/fixtures"
    params = {"league": league_id, "season": season, "status": "FT"}
    resp = requests.get(url, headers=cabeceras_api(), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", [])


def obtener_estadisticas_partido(fixture_id):
    """Devuelve las estadisticas (corners, tarjetas, tiros) de un partido."""
    url = f"{API_BASE_URL}/fixtures/statistics"
    params = {"fixture": fixture_id}
    resp = requests.get(url, headers=cabeceras_api(), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", [])


def extraer_valor_stat(stats_equipo, nombre_stat):
    """Busca un valor concreto (ej. 'Corner Kicks') dentro de las stats de un equipo."""
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
# GUARDAR UN PARTIDO EN LA BASE DE DATOS
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
    print("Conectando a Supabase...")
    conn = conectar_db()
    crear_tablas_si_no_existen(conn)

    total_guardados = 0

    for liga in LIGAS:
        print(f"\n--- Procesando {liga['nombre']} ({liga['pais']}) ---")
        liga_id = guardar_liga_si_no_existe(conn, liga)

        print("Pidiendo partidos finalizados a API-Football...")
        partidos = obtener_partidos_finalizados(liga["api_league_id"], TEMPORADA)
        print(f"Encontrados {len(partidos)} partidos finalizados.")

        for i, fixture in enumerate(partidos, start=1):
            fixture_id = fixture["fixture"]["id"]
            local = fixture["teams"]["home"]["name"]
            visitante = fixture["teams"]["away"]["name"]
            print(f"  [{i}/{len(partidos)}] {local} vs {visitante} (fixture {fixture_id})")

            stats = obtener_estadisticas_partido(fixture_id)
            guardar_partido(conn, liga_id, fixture, stats)
            total_guardados += 1

            time.sleep(PAUSA_ENTRE_LLAMADAS)

    conn.close()
    print(f"\nListo. Se guardaron/actualizaron {total_guardados} partidos en total.")


if __name__ == "__main__":
    main()
