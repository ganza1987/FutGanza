"""
Scheduler: sends automatic pre-match analysis before programmed fixtures.

Fixtures are defined in FIXTURES list (or loaded from a JSON file).
The scheduler checks every hour; if a match starts within ALERT_HOURS hours
and hasn't been analysed yet, it triggers the analysis automatically.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from analyzer import analyze_match
from bot_handler import send_message, split_message

logger = logging.getLogger(__name__)

ALERT_HOURS = int(os.getenv("ALERT_HOURS", "2"))       # How many hours before kick-off to send analysis
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))  # Seconds between checks (default: 1 hour)
CHAT_IDS_ENV = os.getenv("NOTIFY_CHAT_IDS", "")        # Comma-separated chat IDs to notify
FIXTURES_FILE = Path("fixtures.json")                   # Optional external fixtures file

# ── Hardcoded example fixtures (ISO 8601 UTC) ──────────────────────────────
# Replace / extend with a real API (e.g. football-data.org) in a future version
EXAMPLE_FIXTURES = [
    # {"home": "Real Madrid", "away": "Barcelona", "kickoff": "2025-10-26T19:00:00Z"},
]


def load_fixtures() -> list[dict]:
    """Load fixtures from file if available, otherwise use hardcoded list."""
    if FIXTURES_FILE.exists():
        try:
            return json.loads(FIXTURES_FILE.read_text())
        except Exception as e:
            logger.warning(f"Could not load fixtures.json: {e}")
    return EXAMPLE_FIXTURES


def get_notify_chat_ids() -> list[str]:
    if not CHAT_IDS_ENV:
        return []
    return [c.strip() for c in CHAT_IDS_ENV.split(",") if c.strip()]


def make_fixture_key(fixture: dict) -> str:
    return f"{fixture['home']}|{fixture['away']}|{fixture['kickoff']}"


async def start_scheduler():
    """Main scheduler loop."""
    logger.info("Scheduler started.")
    already_sent: set[str] = set()

    while True:
        try:
            await check_fixtures(already_sent)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


async def check_fixtures(already_sent: set):
    chat_ids = get_notify_chat_ids()
    if not chat_ids:
        logger.debug("No NOTIFY_CHAT_IDS configured — skipping scheduler check.")
        return

    fixtures = load_fixtures()
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=ALERT_HOURS)

    for fixture in fixtures:
        key = make_fixture_key(fixture)
        if key in already_sent:
            continue

        try:
            kickoff = datetime.fromisoformat(fixture["kickoff"].replace("Z", "+00:00"))
        except ValueError:
            logger.warning(f"Invalid kickoff format: {fixture['kickoff']}")
            continue

        if now <= kickoff <= window_end:
            logger.info(f"Auto-analysis: {fixture['home']} vs {fixture['away']} at {kickoff}")
            already_sent.add(key)

            home, away = fixture["home"], fixture["away"]
            header = (
                f"🔔 *ANÁLISIS AUTOMÁTICO PRE-PARTIDO*\n"
                f"⏰ Comienza en menos de {ALERT_HOURS}h\n\n"
            )
            report = await analyze_match(home, away)
            full_report = header + report

            for chat_id in chat_ids:
                for chunk in split_message(full_report):
                    await send_message(chat_id, chunk)
                    await asyncio.sleep(0.5)
