"""
Handles bet screenshots sent to the bot.
Uses Claude Vision to extract bet details from the image.
"""
import os
import httpx
import base64
import logging
from database import add_bet, update_bet_result, get_bets

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_API      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


async def download_telegram_photo(file_id: str) -> bytes | None:
    """Download a photo from Telegram and return raw bytes."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Get file path
            r = await client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
            file_path = r.json()["result"]["file_path"]
            # Download file
            r2 = await client.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}")
            return r2.content
    except Exception as e:
        logger.error(f"download_telegram_photo: {e}")
        return None


async def extract_bet_from_image(image_bytes: bytes) -> dict | None:
    """
    Send image to Claude Vision and extract bet details.
    Returns dict with keys: match, market, odds, stake, result, profit
    """
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """Analiza esta captura de pantalla de una apuesta deportiva y extrae los datos en JSON.

Devuelve SOLO un objeto JSON válido con estos campos (usa null si no encuentras el dato):
{
  "match": "Equipo A vs Equipo B",
  "market": "descripción del mercado (ej: +2.5 goles, resultado 1X2, BTTS...)",
  "odds": 1.75,
  "stake": 10.0,
  "result": "won" o "lost" o "void" o "pending",
  "profit": 8.75,
  "confidence": "high" o "medium" o "low"
}

Reglas:
- result: "won" si ganó, "lost" si perdió, "void" si fue anulada, "pending" si no hay resultado aún
- profit: beneficio neto (positivo si ganó, negativo si perdió, 0 si void)
- Si no ves claramente un dato, ponlo como null
- Devuelve SOLO el JSON, sin texto adicional"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 500,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": image_b64,
                    }
                },
                {"type": "text", "text": prompt}
            ]
        }]
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            text = r.json()["content"][0]["text"].strip()
            # Clean possible markdown code blocks
            text = text.replace("```json", "").replace("```", "").strip()
            import json
            return json.loads(text)
    except Exception as e:
        logger.error(f"extract_bet_from_image: {e}")
        return None


async def process_bet_screenshot(chat_id, file_id: str, caption: str | None, send_fn) -> bool:
    """
    Main handler for bet screenshots.
    Downloads image, extracts data, saves to DB, confirms to user.
    Returns True if processed as a bet screenshot.
    """
    await send_fn(chat_id, "🔍 Analizando la captura...")

    # Download image
    image_bytes = await download_telegram_photo(file_id)
    if not image_bytes:
        await send_fn(chat_id, "❌ No pude descargar la imagen. Inténtalo de nuevo.")
        return False

    # Extract bet data
    bet_data = await extract_bet_from_image(image_bytes)
    if not bet_data:
        await send_fn(chat_id, "❌ No pude leer los datos de la apuesta. Asegúrate de que la imagen sea clara.")
        return False

    confidence = bet_data.get("confidence", "low")
    match   = bet_data.get("match")
    market  = bet_data.get("market")
    odds    = bet_data.get("odds")
    stake   = bet_data.get("stake")
    result  = bet_data.get("result", "pending")
    profit  = bet_data.get("profit")

    # If confidence is low, ask for confirmation
    if confidence == "low" or not match or not market:
        await send_fn(chat_id,
            "⚠️ No pude leer todos los datos con claridad.\n\n"
            f"Lo que detecté:\n"
            f"• Partido: {match or 'no detectado'}\n"
            f"• Mercado: {market or 'no detectado'}\n"
            f"• Cuota: {odds or 'no detectado'}\n"
            f"• Importe: {stake or 'no detectado'}€\n"
            f"• Resultado: {result}\n\n"
            "Puedes registrarla manualmente:\n"
            "`/apuesta Partido ; Mercado ; Cuota ; Importe`"
        )
        return True

    # Save to database
    if result in ("won", "lost", "void"):
        # Already settled — save with result
        bet_id = add_bet(
            chat_id=chat_id,
            match=match,
            market=market,
            odds=odds,
            stake=stake,
        )
        update_bet_result(bet_id, result)
    else:
        # Pending
        bet_id = add_bet(
            chat_id=chat_id,
            match=match,
            market=market,
            odds=odds,
            stake=stake,
        )

    # Build confirmation message
    result_emoji = {"won": "✅ Ganada", "lost": "❌ Perdida", "void": "➖ Nula", "pending": "⏳ Pendiente"}.get(result, result)
    profit_str = ""
    if profit is not None:
        sign = "+" if profit >= 0 else ""
        profit_str = f"\n💰 Beneficio: *{sign}{round(profit, 2)}€*"

    await send_fn(chat_id,
        f"✅ *Apuesta registrada* (ID: {bet_id})\n\n"
        f"📌 {match}\n"
        f"🎯 {market}"
        f"{f' · cuota {odds}' if odds else ''}"
        f"{f' · {stake}€' if stake else ''}\n"
        f"📊 {result_emoji}{profit_str}\n\n"
        f"_Confianza de lectura: {confidence}_"
    )
    return True
