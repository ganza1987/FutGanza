"""
Scheduler: sends automatic daily analysis for Asian and American leagues.
- Asian leagues: 6:00 AM Spain time
- American leagues: 10:00 AM Spain time
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

# ── League definitions ─────────────────────────────────────────────────────────

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

AMERICAN_LEAGUES = {
    253: "MLS",
    262: "Liga MX",
    71:  "Brasileirao Serie A",
    72:  "Brasileirao Serie B",
    128: "Liga Profesional Argentina",
    131: "Primera B Nacional Argentina",
    239: "Liga BetPlay Colombia",
    265: "Primera División Chile",
    281: "Liga 1 Perú",
    268: "Liga AUF Uruguay",
    233: "Canadian Premier League",
}

ASIAN_SEND_HOUR    = 6   # 6:00 AM Spain
AMERICAN_SEND_HOUR = 10  # 10:00 AM Spain


def get_notify_chat_ids() -> list[str]:
    if not CHAT_IDS_ENV:
        return []
    return [c.strip() for c in CHAT_IDS_ENV.split(",") if c.strip()]


def spain_offset() -> int:
    """Return UTC offset for Spain (1 in winter, 2 in summer)."""
    now = datetime.now(timezone.utc)
    year = now.year
    dst_start = datetime(year, 3, 31, 1, 0, tzinfo=timezone.utc)
    dst_end   = datetime(year, 10, 27, 1, 0, tzinfo=timezone.utc)
    if dst_start <= now < dst_end:
        return 2
    return 1


def to_utc_hour(spain_hour: int) -> int:
    return spain_hour - spain_offset()


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


async def send_daily_analysis(leagues: dict, region_name: str, region_emoji: str):
    """Generic daily analysis sender for any set of leagues."""
    chat_ids = get_notify_chat_ids()
    if not chat_ids:
        logger.warning("No NOTIFY_CHAT_IDS configured.")
        return

    season = datetime.now(timezone.utc).year
    all_fixtures = []

    logger.info(f"Fetching {region_name} fixtures for {datetime.now(timezone.utc).strftime('%Y-%m-%d')}...")

    for league_id, league_name in leagues.items():
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
            })
        await asyncio.sleep(0.5)

    if not all_fixtures:
        logger.info(f"No {region_name} fixtures today.")
        for chat_id in chat_ids:
            await send_message(chat_id,
                f"{region_emoji} *Análisis diario — {region_name}*\n"
                f"_No hay partidos programados hoy._"
            )
        return

    today_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    header = (
        f"{region_emoji} *ANÁLISIS DIARIO — {region_name.upper()}*\n"
        f"📅 {today_str} · {len(all_fixtures)} partido{'s' if len(all_fixtures) != 1 else ''}\n\n"
        f"_Generando análisis... puede tardar unos minutos._"
    )
    for chat_id in chat_ids:
        await send_message(chat_id, header)

    for i, fix in enumerate(all_fixtures, 1):
        home    = fix["home"]
        away    = fix["away"]
        league  = fix["league"]
        kickoff = fix["kickoff"]

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

        await asyncio.sleep(3)

    for chat_id in chat_ids:
        await send_message(chat_id,
            f"✅ *{region_name} — Análisis completado*\n"
            f"_{len(all_fixtures)} partidos procesados · Usa /stats para tu seguimiento_"
        )


async def send_daily_asian_analysis():
    await send_daily_analysis(ASIAN_LEAGUES, "Ligas Asiáticas", "🌏")


async def send_daily_american_analysis():
    await send_daily_analysis(AMERICAN_LEAGUES, "Ligas Americanas", "🌎")


async def start_scheduler():
    """Main scheduler loop."""
    logger.info("Scheduler started.")
    already_sent: set[str] = set()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_key = now_utc.strftime("%Y-%m-%d")

            # ── Daily Asian analysis at 6 AM Spain ────────────────────────────
            asian_key = f"asian_{today_key}"
            if now_utc.hour == to_utc_hour(ASIAN_SEND_HOUR) and now_utc.minute < 10:
                if asian_key not in already_sent:
                    logger.info(f"Triggering daily Asian analysis for {today_key}")
                    already_sent.add(asian_key)
                    await send_daily_asian_analysis()

            # ── Daily American analysis at 10 AM Spain ────────────────────────
            american_key = f"american_{today_key}"
            if now_utc.hour == to_utc_hour(AMERICAN_SEND_HOUR) and now_utc.minute < 10:
                if american_key not in already_sent:
                    logger.info(f"Triggering daily American analysis for {today_key}")
                    already_sent.add(american_key)
                    await send_daily_american_analysis()

            # ── Manual fixtures from fixtures.json ────────────────────────────
            import json
            from pathlib import Path
            fixtures_file = Path("fixtures.json")
            if fixtures_file.exists():
                try:
                    fixtures = json.loads(fixtures_file.read_text())
                    window_end = now_utc + timedelta(hours=ALERT_HOURS)
                    chat_ids = get_notify_chat_ids()

                    for fix in fixtures:
                        key = f"fix_{fix['home']}|{fix['away']}|{fix['kickoff']}"
                        if key in already_sent:
                            continue
                        try:
                            kickoff = datetime.fromisoformat(fix["kickoff"].replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if now_utc <= kickoff <= window_end:
                            already_sent.add(key)
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

        await asyncio.sleep(60)
