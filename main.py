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
    init_db()
    await set_webhook()
    asyncio.create_task(start_scheduler())
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return {"status": "FutGanza Bot running ⚽"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    await handle_update(data)
    return {"ok": True}
