"""
Scheduler: sends automatic daily analysis ONLY for the leagues that have
match data ingested in Supabase (las 10 ligas de build_database.py).
- Ligas con datos: 06:00 AM Spain time (revision 1)
- Ligas con datos: 12:30 PM Spain time (revision 2, repaso de mediodia)
Also supports manual fixtures via fixtures.json.
"""

import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta

from analyzer import analyze_match, analyze_match_with_picks
from bot_handler import send_message, split_message

logger = logging.getLogger(__name__)

ALERT_HOURS      = int(os.getenv("ALERT_HOURS", "2"))
CHAT_IDS_ENV     = os.getenv("NOTIFY_CHAT_IDS", "")
APIFOOTBALL_KEY  = os.getenv("APIFOOTBALL_KEY", "888285a75737af52283245495c97c67a")
APIFOOTBALL_URL  = "https://v3.football.api-sports.io"

# ── Candado anti-solapamiento: evita que dos analisis corran a la vez ─────────
_analysis_running = False

# ── Picks diarios (ranking de mayor probabilidad, cualquier mercado) ──────────
MIN_CONFIANZA_PICK   = 70   # % minimo para que un pick aparezca en el ranking
MAX_PICKS_MOSTRADOS  = 10   # techo de picks a mostrar (si hay menos, se muestran menos)

# ── League definitions ─────────────────────────────────────────────────────────

# Estas son las 10 ligas que ya tienen partidos guardados en Supabase
# (las mismas que ingiere build_database.py). IDs de API-Football.
# Son las UNICAS ligas que este scheduler analiza automaticamente.
LIGAS_CON_DATOS = {
    113: "Allsvenskan (Suecia)",
    103: "Eliteserien (Noruega)",
    119: "Superliga (Dinamarca)",
    164: "Úrvalsdeild (Islandia)",
    244: "Veikkausliiga (Finlandia)",
    283: "Liga I (Rumanía)",
    169: "Super League (China)",
    98:  "J1 League (Japón)",
    288: "Premier Soccer League (Sudáfrica)",
    253: "Major League Soccer (EEUU)",
}

# Horarios (hora España) para "Ligas con datos": primera revision y repaso de mediodia
LIGAS_CON_DATOS_SEND_HOUR     = 6    # 06:00 AM Spain
LIGAS_CON_DATOS_SEND_HOUR_2   = 12   # 12:30 PM Spain
LIGAS_CON_DATOS_SEND_MINUTE_2 = 30


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


def format_picks_message(all_picks: list[dict], region_name: str) -> str:
    """Construye el mensaje de ranking de picks del dia (independiente del
    mercado): los que superen MIN_CONFIANZA_PICK, ordenados de mayor a menor
    probabilidad, con un maximo de MAX_PICKS_MOSTRADOS."""
    filtrados = [p for p in all_picks if p["probability"] >= MIN_CONFIANZA_PICK]

    if not filtrados:
        return (
            f"🎯 *TOP PICKS DEL DÍA — {region_name.upper()}*\n"
            f"_Ningún pick superó el {MIN_CONFIANZA_PICK}% de confianza hoy._"
        )

    filtrados.sort(key=lambda p: p["probability"], reverse=True)
    top = filtrados[:MAX_PICKS_MOSTRADOS]

    lines = [f"🎯 *TOP PICKS DEL DÍA — {region_name.upper()}* ({len(top)})\n"]
    for i, p in enumerate(top, 1):
        muestra = f" _(muestra: {p['sample']})_" if p.get("sample") else ""
        lines.append(
            f"{i}. *{p['home']} vs {p['away']}* ({p['league']})\n"
            f"   {p['label']}: *{p['probability']}%*{muestra}\n"
            f"   💡 {p['reason']}"
        )
    return "\n\n".join(lines)


async def send_daily_analysis(leagues: dict, region_name: str, region_emoji: str):
    """Punto de entrada publico: evita que dos analisis se ejecuten en paralelo
    (ej. un /picks manual mientras ya esta corriendo el analisis programado, o
    un reintento duplicado de Telegram). Si ya hay uno en curso, avisa y sale."""
    global _analysis_running
    if _analysis_running:
        logger.warning(f"send_daily_analysis({region_name}) omitido: ya hay un analisis en curso.")
        for chat_id in get_notify_chat_ids():
            await send_message(chat_id,
                "⏳ Ya hay un análisis en curso ahora mismo. Espera a que termine antes de lanzar otro."
            )
        return
    _analysis_running = True
    try:
        await _send_daily_analysis_impl(leagues, region_name, region_emoji)
    finally:
        _analysis_running = False


async def _send_daily_analysis_impl(leagues: dict, region_name: str, region_emoji: str):
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

    all_picks: list[dict] = []

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
            report, picks = await analyze_match_with_picks(home, away)
            prefix = f"🏆 *{league}* · ⏰ {ko_str}h\n\n"
            full_report = prefix + report
            for chat_id in chat_ids:
                for chunk in split_message(full_report):
                    await send_message(chat_id, chunk)
                    await asyncio.sleep(0.3)

            for p in picks:
                all_picks.append({**p, "home": home, "away": away, "league": league})
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

    picks_message = format_picks_message(all_picks, region_name)
    for chat_id in chat_ids:
        for chunk in split_message(picks_message):
            await send_message(chat_id, chunk)
            await asyncio.sleep(0.3)


async def send_daily_ligas_con_datos_analysis():
    await send_daily_analysis(LIGAS_CON_DATOS, "Ligas con datos", "📊")


async def send_daily_ligas_con_datos_analysis_mediodia():
    await send_daily_analysis(LIGAS_CON_DATOS, "Ligas con datos (repaso mediodía)", "📊")


async def start_scheduler():
    """Main scheduler loop."""
    logger.info("Scheduler started.")
    already_sent: set[str] = set()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_key = now_utc.strftime("%Y-%m-%d")

            # ── Daily "Ligas con datos" analysis at 06:00 Spain ───────────────
            datos_key = f"datos_{today_key}"
            if now_utc.hour == to_utc_hour(LIGAS_CON_DATOS_SEND_HOUR) and now_utc.minute < 10:
                if datos_key not in already_sent:
                    logger.info(f"Triggering daily 'Ligas con datos' analysis for {today_key}")
                    already_sent.add(datos_key)
                    await send_daily_ligas_con_datos_analysis()

            # ── Daily "Ligas con datos" 2nd check at 12:30 Spain ──────────────
            datos_key_2 = f"datos2_{today_key}"
            utc_hour_2 = to_utc_hour(LIGAS_CON_DATOS_SEND_HOUR_2)
            if (now_utc.hour == utc_hour_2
                    and LIGAS_CON_DATOS_SEND_MINUTE_2 <= now_utc.minute < LIGAS_CON_DATOS_SEND_MINUTE_2 + 10):
                if datos_key_2 not in already_sent:
                    logger.info(f"Triggering midday 'Ligas con datos' check for {today_key}")
                    already_sent.add(datos_key_2)
                    await send_daily_ligas_con_datos_analysis_mediodia()

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
