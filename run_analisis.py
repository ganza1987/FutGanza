"""
Punto de entrada del analisis diario para GitHub Actions (ver
.github/workflows/analisis_manana.yml y analisis_mediodia.yml). Sustituye al
bucle while True de scheduler.py, que mantenia la app de Render despierta 24/7
y agoto las 750 h/mes gratis compartidas entre las 3 apps.

Uso:
    python run_analisis.py manana     # 06:00 Madrid: jornada completa + valor
    python run_analisis.py mediodia   # 12:30 Madrid: solo cuotas + valor

Hora exacta en Madrid todo el ano: GitHub Actions solo entiende UTC y Madrid
cambia entre UTC+1 y UTC+2. Cada workflow se lanza en las dos horas UTC posibles
y este script solo continua si en Madrid es la hora correcta (asi solo una de
las dos ejecuciones hace algo). FORZAR_ANALISIS=true (ejecucion manual) se
salta la comprobacion.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# modo -> (hora de Madrid en la que debe ejecutarse, funcion a llamar)
MODOS = {
    "manana": (6, scheduler.send_daily_ligas_con_datos_analysis),
    "mediodia": (12, scheduler.send_daily_ligas_con_datos_analysis_mediodia),
}


async def main(modo: str):
    hora_objetivo, funcion = MODOS[modo]
    hora_madrid = datetime.now(ZoneInfo("Europe/Madrid")).hour
    if os.getenv("FORZAR_ANALISIS", "").strip().lower() != "true" and hora_madrid != hora_objetivo:
        logger.info(f"Son las {hora_madrid}h en Madrid, no las {hora_objetivo}h: no se hace nada (la otra ejecucion del dia lo hara).")
        return
    await funcion()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODOS:
        raise SystemExit(f"Uso: python run_analisis.py [{'|'.join(MODOS)}]")
    asyncio.run(main(sys.argv[1]))
