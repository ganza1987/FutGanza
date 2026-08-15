import os
import logging
import asyncio
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
import httpx
from bot_handler import handle_update
from scheduler import start_scheduler
from dashboard import router as dashboard_router
from database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")


async def set_webhook():
    if not TELEGRAM_TOKEN or not WEBHOOK_URL:
        logger.warning("TELEGRAM_TOKEN or WEBHOOK_URL not set — skipping webhook registration.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json={"url": f"{WEBHOOK_URL}/webhook"})
        logger.info(f"Webhook set: {r.json()}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
    except Exception as e:
        logger.warning(f"DB init failed: {e}")
    await set_webhook()
    asyncio.create_task(start_scheduler())
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return {"status": "FutGanza Bot running ⚽"}


@app.get("/debug-apif")
async def debug_apif():
    import httpx as _httpx
    apif_key = os.getenv("APIFOOTBALL_KEY", "")
    result = {"key_len": len(apif_key), "key_preview": apif_key[:8] + "..." if apif_key else "VACIA"}
    try:
        headers = {"x-apisports-key": apif_key, "x-rapidapi-host": "v3.football.api-sports.io"}
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://v3.football.api-sports.io/teams", headers=headers, params={"search": "BK Hacken"})
            result["status_code"] = r.status_code
            result["response"] = r.json()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    # Lanzamos el procesamiento en segundo plano y respondemos "ok" al instante.
    # Si esperasemos aqui a que termine (ej. un /picks que tarda varios minutos),
    # Telegram reintentaria el mismo mensaje por timeout, duplicando el analisis.
    asyncio.create_task(handle_update(data))
    return {"ok": True}
