"""
Web dashboard for bet tracking.
Mounted at /dashboard on the FastAPI app.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from database import get_all_bets_web, get_stats

router = APIRouter()

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    bets = get_all_bets_web()
    
    # Aggregate stats
    won = sum(1 for b in bets if b["result"] == "won")
    lost = sum(1 for b in bets if b["result"] == "lost")
    pending = sum(1 for b in bets if b["result"] == "pending")
    void_ = sum(1 for b in bets if b["result"] == "void")
    total_staked = sum(b["stake"] or 0 for b in bets)
    total_profit = sum(b["profit"] or 0 for b in bets)
    total_settled = won + lost
    win_rate = round(won / total_settled * 100, 1) if total_settled else 0
    roi = round(total_profit / total_staked * 100, 1) if total_staked else 0

    # Build rows
    rows_html = ""
    for b in bets:
        result_badge = {
            "won":     '<span class="badge won">✅ Ganada</span>',
            "lost":    '<span class="badge lost">❌ Perdida</span>',
            "pending": '<span class="badge pending">⏳ Pendiente</span>',
            "void":    '<span class="badge void">➖ Nula</span>',
        }.get(b["result"], b["result"])
        
        profit_str = ""
        if b["profit"] is not None:
            sign = "+" if b["profit"] >= 0 else ""
            color = "green" if b["profit"] >= 0 else "red"
            profit_str = f'<span style="color:{color};font-weight:600">{sign}{b["profit"]}€</span>'
        
        odds_str = f"{b['odds']}" if b["odds"] else "—"
        stake_str = f"{b['stake']}€" if b["stake"] else "—"
        date_str = b["created_at"][:10] if b["created_at"] else "—"
        
        rows_html += f"""
        <tr>
            <td>#{b['id']}</td>
            <td>{date_str}</td>
            <td><strong>{b['match']}</strong></td>
            <td>{b['market']}</td>
            <td>{odds_str}</td>
            <td>{stake_str}</td>
            <td>{result_badge}</td>
            <td>{profit_str}</td>
        </tr>"""

    profit_color = "var(--green)" if total_profit >= 0 else "var(--red)"
    profit_sign = "+" if total_profit >= 0 else ""
    roi_color = "var(--green)" if roi >= 0 else "var(--red)"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FutGanza — Dashboard</title>
<style>
  :root {{
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e8eaf0; --muted: #8b8fa8; --accent: #4f8ef7;
    --green: #4caf7d; --red: #e05c5c; --yellow: #f0b429;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, sans-serif; padding: 24px; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 24px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 28px; }}
  .stat-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
  .stat-label {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
  .stat-value {{ font-size: 26px; font-weight: 700; }}
  .table-wrap {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
  .table-header {{ padding: 16px 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }}
  .table-header h2 {{ font-size: 15px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ padding: 10px 16px; text-align: left; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); font-size: 12px; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(255,255,255,0.02); }}
  .badge {{ padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
  .badge.won {{ background: rgba(76,175,125,0.15); color: var(--green); }}
  .badge.lost {{ background: rgba(224,92,92,0.15); color: var(--red); }}
  .badge.pending {{ background: rgba(240,180,41,0.15); color: var(--yellow); }}
  .badge.void {{ background: rgba(139,143,168,0.15); color: var(--muted); }}
  .empty {{ padding: 40px; text-align: center; color: var(--muted); }}
  .telegram-tip {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; margin-bottom: 24px; font-size: 13px; color: var(--muted); }}
  .telegram-tip code {{ background: rgba(79,142,247,0.15); color: var(--accent); padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
<h1>⚽ FutGanza Dashboard</h1>
<p class="subtitle">Seguimiento de apuestas basadas en análisis</p>

<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-label">Total apuestas</div>
    <div class="stat-value">{len(bets)}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Ganadas / Perdidas</div>
    <div class="stat-value">{won} / {lost}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Pendientes</div>
    <div class="stat-value" style="color:var(--yellow)">{pending}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Tasa de acierto</div>
    <div class="stat-value">{win_rate}%</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Capital apostado</div>
    <div class="stat-value">{round(total_staked, 2)}€</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Beneficio neto</div>
    <div class="stat-value" style="color:{profit_color}">{profit_sign}{round(total_profit,2)}€</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">ROI</div>
    <div class="stat-value" style="color:{roi_color}">{profit_sign}{roi}%</div>
  </div>
</div>

<div class="telegram-tip">
  💬 Registra apuestas desde Telegram: <code>/apuesta Real Madrid vs Barça · +2.5 goles · 1.75 · 10</code>
  &nbsp;·&nbsp; Actualiza resultados: <code>/resultado 3 ganó</code>
  &nbsp;·&nbsp; Ver resumen: <code>/stats</code>
</div>

<div class="table-wrap">
  <div class="table-header">
    <h2>📋 Historial de apuestas</h2>
    <span style="color:var(--muted);font-size:12px">{len(bets)} registros</span>
  </div>
  {'<table><thead><tr><th>#</th><th>Fecha</th><th>Partido</th><th>Mercado</th><th>Cuota</th><th>Stake</th><th>Resultado</th><th>Beneficio</th></tr></thead><tbody>' + rows_html + '</tbody></table>' if bets else '<div class="empty">Aún no hay apuestas registradas.<br>Usa /apuesta en Telegram para empezar.</div>'}
</div>

</body>
</html>"""
    return html
