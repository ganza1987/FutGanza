"""
Scheduler: sends automatic daily analysis for Asian leagues at 6:00 AM Spain time.
Also supports manual fixtures via fixtures.json.
"""

import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta

from analyzer import analyze_match
from bot_handler import send_message, split_message

logger = logging.getLogger(__name__)

ALERT_HOURS      = int(os.getenv("ALERT_HOURS", "2"))
CHAT_IDS_ENV     = os.getenv("NOTIFY_CHAT_IDS", "")
APIFOOTBALL_KEY  = os.getenv("APIFOOTBALL_KEY", "888285a75737af52283245495c97c67a")
APIFOOTBALL_URL  = "https://v3.football.api-sports.io"

# ── Asian leagues IDs in API-Football ─────────────────────────────────────────
ASIAN_LEAGUES = {
    292: "K League 1",
    293: "K League 2",
    98:  "J1 League",
    99:  "J2 League",
    169: "Chinese Super League",
    170: "China League One",
    323: "Indian Super League",
    296: "Thai League 1",
    188: "A-League",
    17:  "AFC Champions League",
}

# Spain is UTC+2 in summer (CEST), UTC+1 in winter (CET)
SEND_HOUR_UTC_SUMMER = 4   # 6:00 AM Spain summer = 4:00 UTC
SEND_HOUR_UTC_WINTER = 5   # 6:00 AM Spain winter = 5:00 UTC


def get_notify_chat_ids() -> list[str]:
    if not CHAT_IDS_ENV:
        return []
    return [c.strip() for c in CHAT_IDS_ENV.split(",") if c.strip()]


def spain_offset() -> int:
    """Return UTC offset for Spain (1 in winter, 2 in summer)."""
    now = datetime.now(timezone.utc)
    # DST starts last Sunday March, ends last Sunday October
    year = now.year
    # Simple approximation
    dst_start = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    dst_end   = datetime(year, 10, 27, 1, 0, tzinfo=timezone.utc)
    if dst_start <= now < dst_end:
        return 2
    return 1


def target_utc_hour() -> int:
    return 6 - spain_offset()  # 6 AM Spain → UTC


async def apif_get(endpoint: str, params: dict) -> dict:
    headers = {
        "x-apisports-key": APIFOOTBALL_KEY,
        "x-rapidapi-host": "v3.football.api-sports.io",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{APIFOOTBALL_URL}/{endpoint}", headers=headers, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning(f"apif_get({endpoint}): {e}")
        return {}


async def get_todays_fixtures(league_id: int, season: int) -> list[dict]:
    """Get today's fixtures for a league."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = await apif_get("fixtures", {
        "league": league_id,
        "season": season,
        "date": today,
    })
    return data.get("response", [])


async def send_daily_asian_analysis():
    """Fetch and analyze all Asian league fixtures for today."""
    chat_ids = get_notify_chat_ids()
    if not chat_ids:
        logger.warning("No NOTIFY_CHAT_IDS configured.")
        return

    season = datetime.now(timezone.utc).year
    all_fixtures = []

    logger.info(f"Fetching Asian league fixtures for {datetime.now(timezone.utc).strftime('%Y-%m-%d')}...")

    for league_id, league_name in ASIAN_LEAGUES.items():
        fixtures = await get_todays_fixtures(league_id, season)
        for fix in fixtures:
            home = fix["teams"]["home"]["name"]
            away = fix["teams"]["away"]["name"]
            kickoff = fix["fixture"]["date"]
            all_fixtures.append({
                "home": home,
                "away": away,
                "league": league_name,
                "kickoff": kickoff,
                "league_id": league_id,
            })
        await asyncio.sleep(0.5)  # respect rate limit

    if not all_fixtures:
        logger.info("No Asian fixtures today.")
        for chat_id in chat_ids:
            await send_message(chat_id,
                f"⚽ *Análisis diario — Ligas Asiáticas*\n"
                f"_No hay partidos programados hoy en las ligas asiáticas._"
            )
        return

    # Send header
    today_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    header = (
        f"🌏 *ANÁLISIS DIARIO — LIGAS ASIÁTICAS*\n"
        f"📅 {today_str} · {len(all_fixtures)} partido{'s' if len(all_fixtures) != 1 else ''}\n\n"
        f"_Generando análisis... puede tardar unos minutos._"
    )
    for chat_id in chat_ids:
        await send_message(chat_id, header)

    # Analyze each fixture
    for i, fix in enumerate(all_fixtures, 1):
        home    = fix["home"]
        away    = fix["away"]
        league  = fix["league"]
        kickoff = fix["kickoff"]

        # Parse kickoff time
        try:
            ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            spain_ko = ko + timedelta(hours=spain_offset())
            ko_str = spain_ko.strftime("%H:%M")
        except Exception:
            ko_str = "?"

        logger.info(f"Analyzing [{i}/{len(all_fixtures)}]: {home} vs {away} ({league})")

        try:
            report = await analyze_match(home, away)
            prefix = f"🏆 *{league}* · ⏰ {ko_str}h\n\n"
            full_report = prefix + report

            for chat_id in chat_ids:
                for chunk in split_message(full_report):
                    await send_message(chat_id, chunk)
                    await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"Error analyzing {home} vs {away}: {e}")
            for chat_id in chat_ids:
                await send_message(chat_id,
                    f"❌ Error analizando *{home} vs {away}* ({league})"
                )

        # Small delay between analyses to avoid rate limits
        await asyncio.sleep(3)

    # Send footer summary
    for chat_id in chat_ids:
        await send_message(chat_id,
            f"✅ *Análisis completado* — {len(all_fixtures)} partidos procesados\n"
            f"_Usa /stats para ver tu seguimiento de apuestas_"
        )


async def start_scheduler():
    """Main scheduler loop."""
    logger.info("Scheduler started.")
    already_sent_daily: set[str] = set()
    already_sent_fixtures: set[str] = set()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_key = now_utc.strftime("%Y-%m-%d")
            target_hour = target_utc_hour()

            # ── Daily Asian analysis at 6 AM Spain time ────────────────────────
            if now_utc.hour == target_hour and now_utc.minute < 10:
                if today_key not in already_sent_daily:
                    logger.info(f"Triggering daily Asian analysis for {today_key}")
                    already_sent_daily.add(today_key)
                    await send_daily_asian_analysis()

            # ── Manual fixtures from fixtures.json ─────────────────────────────
            import json
            from pathlib import Path
            fixtures_file = Path("fixtures.json")
            if fixtures_file.exists():
                try:
                    fixtures = json.loads(fixtures_file.read_text())
                    window_end = now_utc + timedelta(hours=ALERT_HOURS)
                    chat_ids = get_notify_chat_ids()

                    for fix in fixtures:
                        key = f"{fix['home']}|{fix['away']}|{fix['kickoff']}"
                        if key in already_sent_fixtures:
                            continue
                        try:
                            kickoff = datetime.fromisoformat(fix["kickoff"].replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if now_utc <= kickoff <= window_end:
                            already_sent_fixtures.add(key)
                            home, away = fix["home"], fix["away"]
                            report = await analyze_match(home, away)
                            header = (
                                f"🔔 *ANÁLISIS PRE-PARTIDO*\n"
                                f"⏰ Comienza en menos de {ALERT_HOURS}h\n\n"
                            )
                            full = header + report
                            for chat_id in chat_ids:
                                for chunk in split_message(full):
                                    await send_message(chat_id, chunk)
                                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"Fixtures error: {e}")

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(60)  # Check every minute
