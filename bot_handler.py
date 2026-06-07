import os
import re
import logging
import httpx

from analyzer import analyze_match
from bet_handler import handle_bet_command

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

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
    text = message.get("text", "").strip()
    if not text:
        return

    # /start o /help
    if text.startswith("/start") or text.startswith("/help"):
        await send_message(chat_id, HELP_TEXT)
        return

    # Bet commands (/apuesta, /resultado, /stats, /apuestas, /web)
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
        "O usa /help para ver todos los comandos."
    )


HELP_TEXT = """
🤖 *FutGanza Bot*

*Análisis de partidos:*
Escribe el partido: `Real Madrid vs Barcelona`

*Apuestas:*
/apuesta Partido ; Mercado ; Cuota ; Importe
_Ejemplo: /apuesta España vs Francia ; +2.5 goles ; 1.80 ; 10_

/resultado <id> ganó|perdió|nula
_Ejemplo: /resultado 3 ganó_

/apuestas — últimas 10 apuestas
/apuestas pendientes — solo pendientes
/stats — tus estadísticas completas
/web — enlace al dashboard web

*Info:*
/help — esta ayuda
""".strip()
