"""
Handles bet-related Telegram commands.

Commands:
  /apuesta <partido> · <mercado> · <cuota> · <importe>
  /resultado <id> ganó|perdió|nula
  /stats
  /apuestas [pendientes]
  /web  — sends link to dashboard
"""
import re
import os
from database import add_bet, update_bet_result, get_bets, get_stats, get_bet_by_id

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

RESULT_MAP = {
    "ganó": "won", "gano": "won", "won": "won", "win": "won", "✅": "won",
    "perdió": "lost", "perdio": "lost", "lost": "lost", "❌": "lost",
    "nula": "void", "void": "void", "anulada": "void",
}


def parse_bet(text: str) -> dict | None:
    """
    Parse: /apuesta Real Madrid vs Barça · +2.5 goles · 1.75 · 10
    Returns dict or None if invalid.
    """
    text = text.replace("/apuesta", "").strip()
    parts = [p.strip() for p in re.split(r"[·|,;]", text) if p.strip()]
    if len(parts) < 2:
        return None

    bet = {"match": parts[0], "market": parts[1] if len(parts) > 1 else None,
           "odds": None, "stake": None}

    for p in parts[2:]:
        try:
            val = float(p.replace("€", "").replace("$", "").strip())
            if val < 20:  # likely odds
                bet["odds"] = val
            else:         # likely stake
                bet["stake"] = val
        except ValueError:
            pass
    return bet


def fmt_result_emoji(result: str) -> str:
    return {"won": "✅", "lost": "❌", "void": "➖", "pending": "⏳"}.get(result, "❓")


def fmt_profit(profit) -> str:
    if profit is None:
        return ""
    sign = "+" if profit >= 0 else ""
    return f" ({sign}{profit}€)"


async def handle_bet_command(chat_id, text: str, send_fn) -> bool:
    """
    Returns True if command was handled, False if not a bet command.
    """
    text = text.strip()

    # /apuesta
    if text.lower().startswith("/apuesta"):
        bet = parse_bet(text)
        if not bet:
            await send_fn(chat_id,
                "❌ Formato incorrecto. Usa:\n"
                "`/apuesta Partido ; Mercado ; Cuota ; Importe`\n\n"
                "Ejemplo:\n`/apuesta Real Madrid vs Barça ; +2.5 goles ; 1.75 ; 10`"
            )
            return True
        bet_id = add_bet(
            chat_id=chat_id,
            match=bet["match"],
            market=bet["market"],
            odds=bet["odds"],
            stake=bet["stake"],
        )
        odds_str = f" ; cuota {bet['odds']}" if bet["odds"] else ""
        stake_str = f" ; {bet['stake']}€" if bet["stake"] else ""
        await send_fn(chat_id,
            f"✅ *Apuesta registrada* (ID: {bet_id})\n\n"
            f"📌 {bet['match']}\n"
            f"🎯 Mercado: {bet['market']}{odds_str}{stake_str}\n\n"
            f"Cuando conozcas el resultado:\n"
            f"`/resultado {bet_id} ganó` o `/resultado {bet_id} perdió`"
        )
        return True

    # /resultado <id> <resultado>
    if text.lower().startswith("/resultado"):
        parts = text.split()
        if len(parts) < 3:
            await send_fn(chat_id,
                "❌ Usa: `/resultado <id> ganó|perdió|nula`\n"
                "Ejemplo: `/resultado 3 ganó`"
            )
            return True
        try:
            bet_id = int(parts[1])
        except ValueError:
            await send_fn(chat_id, "❌ El ID debe ser un número.")
            return True

        result_raw = parts[2].lower()
        result = RESULT_MAP.get(result_raw)
        if not result:
            await send_fn(chat_id, "❌ Resultado válido: `ganó`, `perdió`, `nula`")
            return True

        bet = get_bet_by_id(bet_id, str(chat_id))
        if not bet:
            await send_fn(chat_id, f"❌ No encontré la apuesta #{bet_id}.")
            return True

        update_bet_result(bet_id, result)
        emoji = fmt_result_emoji(result)
        await send_fn(chat_id,
            f"{emoji} *Apuesta #{bet_id} actualizada*\n"
            f"📌 {bet['match']} — {bet['market']}\n"
            f"Resultado: *{'Ganada' if result=='won' else 'Perdida' if result=='lost' else 'Nula'}*"
        )
        return True

    # /stats
    if text.lower().startswith("/stats"):
        s = get_stats(str(chat_id))
        total = s["won"] + s["lost"]
        profit_sign = "+" if s["total_profit"] >= 0 else ""
        roi_sign = "+" if s["roi"] >= 0 else ""
        bar_filled = int(s["win_rate"] / 10) if total else 0
        bar = "█" * bar_filled + "░" * (10 - bar_filled)

        await send_fn(chat_id,
            f"📊 *Tus estadísticas*\n\n"
            f"Total apuestas: {s['total_bets']} ({s['pending']} pendientes)\n"
            f"✅ Ganadas: {s['won']} · ❌ Perdidas: {s['lost']} · ➖ Nulas: {s['void']}\n\n"
            f"Tasa de acierto: [{bar}] {s['win_rate']}%\n"
            f"Capital apostado: {s['total_staked']}€\n"
            f"Beneficio neto: *{profit_sign}{s['total_profit']}€*\n"
            f"ROI: *{roi_sign}{s['roi']}%*\n\n"
            f"🌐 Dashboard: {WEBHOOK_URL}/dashboard"
        )
        return True

    # /apuestas
    if text.lower().startswith("/apuestas"):
        only_pending = "pendiente" in text.lower()
        bets = get_bets(str(chat_id), limit=10,
                        result_filter="pending" if only_pending else None)
        if not bets:
            await send_fn(chat_id, "No tienes apuestas registradas aún.")
            return True

        lines = [f"📋 *Últimas {'pendientes' if only_pending else 'apuestas'}*\n"]
        for b in bets:
            emoji = fmt_result_emoji(b["result"])
            odds_str = f" @{b['odds']}" if b["odds"] else ""
            stake_str = f" · {b['stake']}€" if b["stake"] else ""
            profit_str = fmt_profit(b["profit"])
            lines.append(
                f"{emoji} *#{b['id']}* {b['match']}\n"
                f"   └ {b['market']}{odds_str}{stake_str}{profit_str}"
            )
        await send_fn(chat_id, "\n".join(lines))
        return True

    # /web
    if text.lower().startswith("/web"):
        await send_fn(chat_id,
            f"🌐 *Dashboard de apuestas*\n{WEBHOOK_URL}/dashboard"
        )
        return True

    return False
