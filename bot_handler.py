import os
import re
import logging
import httpx

from analyzer import analyze_match
from bet_handler import handle_bet_command
from image_bet_handler import process_bet_screenshot

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "")

VS_PATTERN = re.compile(
    r"^(.+?)\s+(?:vs\.?|versus|contra|-)\s+(.+)$",
    re.IGNORECASE | re.UNICODE
)


async def send_message(chat_id, text: str, parse_mode: str = "Markdown"):
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if r.status_code != 200:
            logger.error(f"Telegram sendMessage error: {r.text}")


async def send_typing(chat_id):
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"}
        )


def split_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def handle_update(data: dict):
    message = data.get("message") or data.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]

    # ── Handle photo messages ──────────────────────────────────────────────────
    if message.get("photo"):
        # Get highest resolution photo
        photo = message["photo"][-1]
        file_id = photo["file_id"]
        caption = message.get("caption", "")
        await send_typing(chat_id)
        await process_bet_screenshot(chat_id, file_id, caption, send_message)
        return

    # ── Handle text messages ───────────────────────────────────────────────────
    text = message.get("text", "").strip()
    if not text:
        return

    # /start or /help
    if text.startswith("/start") or text.startswith("/help"):
        await send_message(chat_id, HELP_TEXT)
        return

    # /picks — lanza el analisis completo (informes + ranking de picks) al momento
    if text.startswith("/picks"):
        await send_message(chat_id,
            "📊 Lanzando análisis de hoy (informes + picks)...\n"
            "_Esto puede tardar varios minutos según los partidos del día._"
        )
        from scheduler import send_daily_ligas_con_datos_analysis
        await send_daily_ligas_con_datos_analysis()
        return

    # /asian — alias de /picks (nombre heredado, ver nota mas abajo)
    # (se mantiene el nombre /asian por compatibilidad con lo que ya usabas,
    # pero ahora mismo solo existe un bloque de ligas: "Ligas con datos")
    if text.startswith("/asian"):
        await send_message(chat_id,
            "📊 Lanzando análisis de ligas con datos...\n"
            "_Esto puede tardar varios minutos según los partidos del día._"
        )
        from scheduler import send_daily_ligas_con_datos_analysis
        await send_daily_ligas_con_datos_analysis()
        return

    # /america — alias del mismo análisis (ver nota en /asian)
    if text.startswith("/america"):
        await send_message(chat_id,
            "📊 Lanzando análisis de ligas con datos...\n"
            "_Esto puede tardar varios minutos según los partidos del día._"
        )
        from scheduler import send_daily_ligas_con_datos_analysis
        await send_daily_ligas_con_datos_analysis()
        return

    # /precision — precision historica de los picks (backtesting)
    if text.startswith("/precision"):
        await send_message(chat_id, _format_precision_message())
        return

    # Bet commands
    handled = await handle_bet_command(chat_id, text, send_message)
    if handled:
        return

    # Match analysis: "Team A vs Team B"
    match = VS_PATTERN.match(text)
    if match:
        home = match.group(1).strip()
        away = match.group(2).strip()
        await send_typing(chat_id)
        await send_message(
            chat_id,
            f"⚽ Analizando *{home}* vs *{away}*...\n_Esto puede tardar unos segundos._"
        )
        await send_typing(chat_id)
        report = await analyze_match(home, away)
        for chunk in split_message(report):
            await send_message(chat_id, chunk)
        return

    await send_message(
        chat_id,
        "No reconozco ese formato.\n"
        "Escribe el partido así: `Real Madrid vs Barcelona`\n"
        "O envía una *captura de tu apuesta* para registrarla automáticamente.\n"
        "Usa /help para ver todos los comandos."
    )


def _format_precision_message() -> str:
    """Construye el mensaje de /precision: acierto global, por tipo de
    condicion y por rango de confianza, a partir del historial de picks
    ya verificados contra el resultado real."""
    from database import get_picks_accuracy_stats
    try:
        stats = get_picks_accuracy_stats()
    except Exception as e:
        logger.error(f"/precision: error leyendo estadisticas: {e}")
        return "❌ No se pudo consultar la precisión ahora mismo. Inténtalo de nuevo en un momento."

    if stats["total"] == 0:
        return (
            "📊 *Precisión de los picks*\n\n"
            "Todavía no hay picks verificados contra un resultado real.\n"
            "Los picks se comprueban automáticamente unas horas después de que "
            "termine cada partido — vuelve a intentarlo en un día o dos."
        )

    lines = [
        "📊 *Precisión de los picks*",
        f"_Basado en {stats['total']} picks verificados_\n",
        f"🎯 *Acierto global: {stats['win_rate']}%* ({stats['total_hit']}/{stats['total']})",
    ]

    if stats["por_rango"]:
        lines.append("\n*Por rango de confianza:*")
        for rango in ["90-100%", "80-89%", "70-79%"]:
            r = stats["por_rango"].get(rango)
            if not r:
                continue
            wr = round(r["hit"] / r["total"] * 100, 1) if r["total"] else 0
            lines.append(f"  {rango} → {wr}% acierto ({r['hit']}/{r['total']})")

    if stats["por_condicion"]:
        lines.append("\n*Por tipo de condición:*")
        condiciones_ordenadas = sorted(
            stats["por_condicion"].items(),
            key=lambda kv: kv[1]["hit"] / kv[1]["total"] if kv[1]["total"] else 0,
            reverse=True,
        )
        for cid, c in condiciones_ordenadas:
            wr = round(c["hit"] / c["total"] * 100, 1) if c["total"] else 0
            lines.append(f"  {c['label']} → {wr}% ({c['hit']}/{c['total']})")

    lines.append(
        "\n_Nota: condiciones sobre forma/historial previo (no sobre el "
        "resultado de ese partido) no se cuentan aquí por no ser verificables._"
    )

    return "\n".join(lines)


HELP_TEXT = """
🤖 *FutGanza Bot*

*Análisis de partidos:*
Escribe el partido: `Real Madrid vs Barcelona`

*Registrar apuesta:*
📸 Envía una captura de tu apuesta y la registro automáticamente

O manualmente:
`/apuesta Partido ; Mercado ; Cuota ; Importe`
_Ejemplo: /apuesta España vs Francia ; +2.5 goles ; 1.80 ; 10_

*Gestionar apuestas:*
/resultado <id> ganó|perdió|nula
/apuestas — últimas 10 apuestas
/apuestas pendientes — solo pendientes
/stats — tus estadísticas completas
/web — enlace al dashboard web

/help — esta ayuda
/picks — analiza todos los partidos de hoy ahora mismo (informes + ranking de picks)
/precision — precisión histórica de los picks (backtesting)
/asian — alias de /picks (nombre heredado)
/america — alias de /picks (nombre heredado)
""".strip()
