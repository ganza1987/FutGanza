import os
import requests
import time
from datetime import datetime, timezone

API_KEY = os.getenv("APIFOOTBALL_KEY", "")
FIXTURE_ID = 1494220  # BK Häcken vs AIK Stockholm

HEADERS = {"x-apisports-key": API_KEY}
URL_STATS = "https://v3.football.api-sports.io/fixtures/statistics"
URL_FIXTURE = "https://v3.football.api-sports.io/fixtures"

def obtener_info_partido():
    r = requests.get(URL_FIXTURE, headers=HEADERS, params={"id": FIXTURE_ID})
    return r.json()["response"][0]

def obtener_estadisticas():
    r = requests.get(URL_STATS, headers=HEADERS, params={"fixture": FIXTURE_ID})
    data = r.json()["response"]
    resumen = {}
    for equipo in data:
        nombre = equipo["team"]["name"]
        stats = {s["type"]: s["value"] for s in equipo["statistics"]}
        resumen[nombre] = stats
    return resumen

# --- Paso 1: consulta única para saber la hora del partido ---
print("Consultando hora del partido...")
info = obtener_info_partido()
fecha_partido_str = info["fixture"]["date"]  # ej: "2026-07-27T17:00:00+00:00"
fecha_partido = datetime.fromisoformat(fecha_partido_str)
ahora = datetime.now(timezone.utc)

segundos_para_empezar = (fecha_partido - ahora).total_seconds()

if segundos_para_empezar > 0:
    minutos = int(segundos_para_empezar // 60)
    print(f"El partido empieza en {minutos} minutos ({fecha_partido_str}).")
    print("Durmiendo sin hacer llamadas hasta 2 minutos antes del inicio...")
    # Dormimos hasta 2 minutos antes, dejando margen de seguridad
    time.sleep(max(segundos_para_empezar - 120, 0))
else:
    print("El partido ya deberia haber empezado o esta en curso.")

# --- Paso 2: polling activo ---
print("\nIniciando polling activo del partido...")
print("Presiona Ctrl+C para detener.\n")

while True:
    try:
        info = obtener_info_partido()
        status = info["fixture"]["status"]["short"]
        minuto = info["fixture"]["status"]["elapsed"]
        gl = info["goals"]["home"]
        gv = info["goals"]["away"]
        hora_actual = datetime.now().strftime("%H:%M:%S")

        if status == "NS":
            print(f"[{hora_actual}] Aun no ha comenzado, reintentando en 30s...")
            time.sleep(30)
            continue
        elif status in ("1H", "2H", "HT"):
            stats = obtener_estadisticas()
            print(f"[{hora_actual}] Minuto {minuto} | Marcador: {gl}-{gv} | Status: {status}")
            for equipo, s in stats.items():
                corners = s.get("Corner Kicks", "N/A")
                amarillas = s.get("Yellow Cards", "N/A")
                rojas = s.get("Red Cards", "N/A")
                tiros = s.get("Total Shots", "N/A")
                print(f"    {equipo}: Corners={corners} Amarillas={amarillas} Rojas={rojas} Tiros={tiros}")
        elif status == "FT":
            print(f"[{hora_actual}] Partido finalizado. Marcador final: {gl}-{gv}")
            break
        else:
            print(f"[{hora_actual}] Status: {status}")

        time.sleep(60)

    except KeyboardInterrupt:
        print("\nDetenido manualmente.")
        break
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(60)