import os
import re
import logging
import httpx

from analyzer import analyze_match

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Patterns to detect a match request: "Team A vs Team B"
VS_PATTERN = re.compile(
    r"^(.+?)\s+(?:vs\.?|versus|contra|-)\s+(.+)$",
    re.IGNORECASE | re.UNICODE
)


async def send_message(chat_id: int | str, text: str, parse_mode: str = "Markdown"):
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if r.status_code != 200:
            logger.error(f"Telegram sendMessage error: {r.text}")


async def send_typing(chat_id: int | str):
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"}
        )


async def handle_update(data: dict):
    message = data.get("message") or data.get("edited_message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if not text:
        return

    # /start or /help
    if text.startswith("/start") or text.startswith("/help"):
        await send_message(chat_id, HELP_TEXT)
        return

    # /condiciones — future: configure weights
    if text.startswith("/condiciones"):
        await send_message(chat_id, CONDITIONS_TEXT)
        return

    # Detect "Team A vs Team B"
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
        # Split if too long (Telegram max 4096 chars)
        for chunk in split_message(report):
            await send_message(chat_id, chunk)
        return

    # Default response
    await send_message(
        chat_id,
        "No reconozco ese formato.\nEscribe el partido así:\n`Real Madrid vs Barcelona`"
    )


def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split long messages into Telegram-safe chunks."""
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


HELP_TEXT = """
🤖 *Football Analysis Bot*

*¿Cómo usarlo?*
Escribe el partido que quieras analizar:
`Real Madrid vs Barcelona`
`España vs Francia`
`Manchester City vs Arsenal`

*Comandos disponibles:*
/help — Esta ayuda
/condiciones — Ver y editar condiciones de análisis

*¿Qué recibirás?*
• Forma reciente de ambos equipos (últimos 5 partidos)
• Estadísticas ofensivas y defensivas
• Tendencias que se cumplen / no se cumplen
• Valoración global del enfrentamiento

_Próximamente: análisis automáticos pre-partido y pesos personalizados por condición._
""".strip()

CONDITIONS_TEXT = """
⚙️ *Condiciones de análisis*

Las condiciones son criterios estadísticos que se evalúan para cada partido.

*Condiciones actuales (por defecto):*
• ✅ Ambos equipos marcan (BTTS)
• ✅ Más de 2.5 goles totales
• ✅ El local tiene mejor forma reciente
• ✅ El visitante tiene más de 1.5 goles/partido
• ✅ Al menos un equipo sin victoria en últimos 3

_Próximamente podrás configurar y ponderar estas condiciones._
""".strip()
