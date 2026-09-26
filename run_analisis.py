"""
Punto de entrada del analisis diario para GitHub Actions (ver
.github/workflows/analisis_manana.yml y analisis_mediodia.yml). Sustituye al
bucle while True de scheduler.py, que mantenia la app de Render despierta 24/7
y agoto las 750 h/mes gratis compartidas entre las 3 apps.

Uso:
    python run_analisis.py manana     # jornada completa + valor (desde las 6 Madrid)
    python run_analisis.py mediodia   # solo cuotas + valor (desde las 12 Madrid)

Envio a prueba de retrasos (2026-09-26): GitHub Actions solo entiende UTC y
retrasa los cron programados HORAS (medido en BaloncestoGanza: llegaban ~5-6 h
tarde). La version anterior exigia que en Madrid fuera EXACTAMENTE la hora
objetivo y, si el cron llegaba tarde, no hacia nada y salia en verde. Ahora cada
modo acepta una VENTANA ancha (manana: 6-12h; mediodia: 12-20h en Madrid) y
reserva "hoy ya se hizo" en la base de datos (tabla avisos_enviados): el primero
en llegar trabaja y los demas ven que ya esta, sin duplicar. Los workflows usan
minutos "raros" (:17/:47), mucho menos saturados que :00, y varios intentos.

Si Telegram rechaza algun envio o falta NOTIFY_CHAT_IDS, la ejecucion termina en
ROJO (GitHub avisa por correo) y la reserva se libera para reintentar. Cada
resultado se anota tambien como anotacion (::notice::/::error::) visible en la
pantalla resumen de la ejecucion. FORZAR_ANALISIS=true (ejecucion manual) se
salta la ventana y la reserva.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import bot_handler
import database
import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# modo -> (hora de Madrid en que empieza la ventana, en que acaba, funcion a llamar)
MODOS = {
    "manana": (6, 12, scheduler.send_daily_ligas_con_datos_analysis),
    "mediodia": (12, 20, scheduler.send_daily_ligas_con_datos_analysis_mediodia),
}


def anotar(nivel: str, mensaje: str) -> None:
    """Anotacion de GitHub Actions: sale en la pantalla resumen de la ejecucion."""
    print(f"::{nivel}::{mensaje}", flush=True)


async def main(modo: str):
    inicio, fin, funcion = MODOS[modo]
    forzar = os.getenv("FORZAR_ANALISIS", "").strip().lower() == "true"
    ahora = datetime.now(ZoneInfo("Europe/Madrid"))

    if not forzar and not (inicio <= ahora.hour < fin):
        anotar("notice", f"Fuera de ventana ({ahora:%H:%M} en Madrid, '{modo}' va de {inicio} a {fin} h): no se hace nada.")
        return

    if not scheduler.get_notify_chat_ids():
        anotar("error", "NOTIFY_CHAT_IDS esta vacio: revisa el secreto en GitHub (Settings > Secrets > Actions).")
        raise SystemExit(1)

    fecha = ahora.date().isoformat()
    reservado = False
    if not forzar:
        if not database.reservar_aviso(modo, fecha):
            anotar("notice", f"'{modo}' de {fecha} ya se hizo en otra ejecucion: no se repite.")
            return
        reservado = True

    try:
        await funcion()
    except Exception as e:
        if reservado:
            database.liberar_aviso(modo, fecha)
        anotar("error", f"'{modo}': error inesperado: {e}")
        raise SystemExit(1)

    if bot_handler.envios_fallidos:
        if reservado:
            database.liberar_aviso(modo, fecha)
        anotar("error", f"Telegram RECHAZO {bot_handler.envios_fallidos} envio(s) (token o chat id incorrectos?): ver el registro.")
        raise SystemExit(1)
    anotar("notice", f"'{modo}' de {fecha} completado.")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODOS:
        raise SystemExit(f"Uso: python run_analisis.py [{'|'.join(MODOS)}]")
    asyncio.run(main(sys.argv[1]))
