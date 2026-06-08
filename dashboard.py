"""
Web dashboard for bet tracking.
Mounted at /dashboard on the FastAPI app.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from database import get_all_bets_web

router = APIRouter()

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    bets = get_all_bets_web()

    won = sum(1 for b in bets if b["result"] == "won")
    lost = sum(1 for b in bets if b["result"] == "lost")
    pending = sum(1 for b in bets if b["result"] == "pending")
    total_staked = sum(b["stake"] or 0 for b in bets)
    total_profit = sum(b["profit"] or 0 for b in bets)
    total_settled = won + lost
    win_rate = round(won / total_settled * 100, 1) if total_settled else 0
    roi = round(total_profit / total_staked * 100, 1) if total_staked else 0
    profit_sign = "+" if total_profit >= 0 else ""
    roi_sign = "+" if roi >= 0 else ""

    # Build cumulative profit series (chronological order)
    sorted_bets = sorted(bets, key=lambda b: b["created_at"] or "")
    cumulative = []
    running = 0
    for b in sorted_bets:
        if b["profit"] is not None:
            running += b["profit"]
            cumulative.append(round(running, 2))

    cumulative_js = str(cumulative) if cumulative else "[0]"
    labels_js = str(list(range(1, len(cumulative) + 1))) if cumulative else "[1]"

    # Build rows html
    rows_html = ""
    for b in bets:
        result_badge = {
            "won":     '<span class="badge bw">Ganada</span>',
            "lost":    '<span class="badge bl">Perdida</span>',
            "pending": '<span class="badge bp">Pendiente</span>',
            "void":    '<span class="badge bv">Nula</span>',
        }.get(b["result"], b["result"])

        if b["profit"] is not None:
            sign = "+" if b["profit"] >= 0 else ""
            col = "#22c55e" if b["profit"] >= 0 else "#ef4444"
            profit_str = f'<span style="color:{col};font-weight:500">{sign}{round(b["profit"],2)}€</span>'
        else:
            profit_str = '<span style="color:#4a6070">—</span>'

        odds_str = str(b["odds"]) if b["odds"] else "—"
        stake_str = f"{b['stake']}€" if b["stake"] else "—"
        date_str = str(b["created_at"])[:10] if b["created_at"] else "—"
        match_short = b["match"][:28] + "…" if len(b["match"]) > 28 else b["match"]
        market_short = b["market"][:22] + "…" if len(b["market"]) > 22 else b["market"]

        rows_html += f"""<tr>
            <td style="color:#4a6070">#{b['id']}</td>
            <td style="color:#4a6070">{date_str}</td>
            <td style="color:#c8d6e5">{match_short}</td>
            <td style="color:#9ab0c4">{market_short}</td>
            <td>{odds_str}</td>
            <td>{stake_str}</td>
            <td>{result_badge}</td>
            <td>{profit_str}</td>
        </tr>"""

    profit_color = "#22c55e" if total_profit >= 0 else "#ef4444"
    roi_color = "#22c55e" if roi >= 0 else "#ef4444"
    last_bet_date = bets[0]["created_at"][:10] if bets else "—"

    empty_block = '<div style="padding:40px;text-align:center;color:#4a6070;font-size:13px">Aún no hay apuestas registradas.<br>Usa /apuesta en Telegram para empezar.</div>'
    table_block = f"""<table>
        <thead><tr>
            <th style="width:40px">#</th>
            <th style="width:90px">Fecha</th>
            <th>Partido</th>
            <th>Mercado</th>
            <th style="width:55px">Cuota</th>
            <th style="width:55px">Stake</th>
            <th style="width:90px">Estado</th>
            <th style="width:70px">P/G</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
    </table>""" if bets else empty_block

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FutGanza — Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1520;color:#c8d6e5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;min-height:100vh}}
.header{{padding:16px 24px 14px;border-bottom:1px solid #1a2535;display:flex;align-items:center;justify-content:space-between}}
.header-left{{display:flex;align-items:center;gap:10px}}
.logo{{width:30px;height:30px;background:#1a3a5c;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:16px}}
.header h1{{font-size:16px;font-weight:600;color:#e8f0f8}}
.header p{{font-size:11px;color:#4a6070;margin-top:2px}}
.header-right{{font-size:11px;color:#4a6070}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid #1a2535}}
.stat{{background:#111d2a;padding:14px 18px;border-right:1px solid #1a2535}}
.stat:last-child{{border-right:none}}
.stat .lbl{{font-size:10px;color:#4a6070;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:7px}}
.stat .val{{font-size:22px;font-weight:600;color:#e8f0f8}}
.stat .sub{{font-size:11px;color:#4a6070;margin-top:3px}}
.charts{{display:grid;grid-template-columns:1fr 250px;border-bottom:1px solid #1a2535}}
.chart-box{{background:#111d2a;padding:16px 20px;border-right:1px solid #1a2535}}
.chart-box2{{background:#111d2a;padding:16px 20px}}
.chart-title{{font-size:10px;color:#4a6070;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:12px}}
.legend{{display:flex;gap:12px;font-size:11px;color:#4a6070;margin-bottom:10px;flex-wrap:wrap}}
.legend span{{display:flex;align-items:center;gap:5px}}
.dot{{width:8px;height:8px;border-radius:2px;flex-shrink:0}}
.history{{background:#111d2a;padding:16px 20px}}
.history-hdr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
.history-hdr h2{{font-size:10px;color:#4a6070;text-transform:uppercase;letter-spacing:0.06em;font-weight:400}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
th{{text-align:left;padding:7px 8px;color:#4a6070;font-weight:400;border-bottom:1px solid #1a2535;font-size:11px}}
td{{padding:9px 8px;border-bottom:1px solid #0d1520;color:#9ab0c4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(255,255,255,0.015)}}
.badge{{display:inline-block;border-radius:3px;padding:2px 8px;font-size:10px;font-weight:500}}
.bw{{background:#0a2a15;color:#22c55e}}
.bl{{background:#2a0a0a;color:#ef4444}}
.bp{{background:#2a2000;color:#f59e0b}}
.bv{{background:#1a1a2a;color:#6b7e8f}}
.hint{{background:#111d2a;border-top:1px solid #1a2535;padding:12px 24px;font-size:11px;color:#4a6070;display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.cmd{{background:#1a2d42;color:#60a5fa;border-radius:3px;padding:2px 7px;font-family:monospace;font-size:11px}}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">⚽</div>
    <div>
      <h1>FutGanza</h1>
      <p>Seguimiento de apuestas</p>
    </div>
  </div>
  <div class="header-right">Última apuesta: {last_bet_date}</div>
</div>

<div class="stats">
  <div class="stat">
    <div class="lbl">Total apuestas</div>
    <div class="val">{len(bets)}</div>
    <div class="sub">{pending} pendientes</div>
  </div>
  <div class="stat">
    <div class="lbl">Tasa de acierto</div>
    <div class="val" style="color:#22c55e">{win_rate}%</div>
    <div class="sub">{won} gan · {lost} per</div>
  </div>
  <div class="stat">
    <div class="lbl">Beneficio neto</div>
    <div class="val" style="color:{profit_color}">{profit_sign}{round(total_profit,2)}€</div>
    <div class="sub">Capital: {round(total_staked,2)}€</div>
  </div>
  <div class="stat">
    <div class="lbl">ROI</div>
    <div class="val" style="color:{roi_color}">{roi_sign}{roi}%</div>
    <div class="sub">Desde el inicio</div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <div class="chart-title">Evolución del beneficio</div>
    <div style="position:relative;width:100%;height:180px">
      <canvas id="lineChart"></canvas>
    </div>
  </div>
  <div class="chart-box2">
    <div class="chart-title">Distribución</div>
    <div class="legend">
      <span><span class="dot" style="background:#22c55e"></span>Ganadas ({won})</span>
      <span><span class="dot" style="background:#ef4444"></span>Perdidas ({lost})</span>
      <span><span class="dot" style="background:#f59e0b"></span>Pendientes ({pending})</span>
    </div>
    <div style="position:relative;width:100%;height:148px">
      <canvas id="donutChart"></canvas>
    </div>
  </div>
</div>

<div class="history">
  <div class="history-hdr">
    <h2>Historial de apuestas</h2>
    <span style="color:#4a6070;font-size:11px">{len(bets)} registros</span>
  </div>
  {table_block}
</div>

<div class="hint">
  💬 Registra: <span class="cmd">/apuesta Real Madrid vs Barça · +2.5 goles · 1.75 · 10</span>
  &nbsp;·&nbsp; Resultado: <span class="cmd">/resultado 3 ganó</span>
  &nbsp;·&nbsp; Resumen: <span class="cmd">/stats</span>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
const lineData = {cumulative_js};
const lineLabels = {labels_js};
new Chart(document.getElementById('lineChart'),{{
  type:'line',
  data:{{
    labels:lineLabels,
    datasets:[{{
      data:lineData,
      borderColor:'#22c55e',
      borderWidth:2,
      pointRadius:0,
      pointHoverRadius:4,
      pointHoverBackgroundColor:'#22c55e',
      fill:true,
      backgroundColor:'rgba(34,197,94,0.07)',
      tension:0.4
    }}]
  }},
  options:{{
    responsive:true,
    maintainAspectRatio:false,
    plugins:{{legend:{{display:false}},tooltip:{{
      backgroundColor:'#1a2d42',
      titleColor:'#9ab0c4',
      bodyColor:'#22c55e',
      borderColor:'#1e3a54',
      borderWidth:1,
      callbacks:{{label:ctx=>(ctx.raw>=0?'+':'')+ctx.raw.toFixed(2)+'€'}}
    }}}},
    scales:{{
      x:{{display:false}},
      y:{{
        grid:{{color:'rgba(255,255,255,0.04)'}},
        ticks:{{color:'#4a6070',font:{{size:10}},callback:v=>(v>=0?'+':'')+v+'€'}},
        border:{{display:false}}
      }}
    }}
  }}
}});

new Chart(document.getElementById('donutChart'),{{
  type:'doughnut',
  data:{{
    labels:['Ganadas','Perdidas','Pendientes'],
    datasets:[{{
      data:[{won},{lost},{pending}],
      backgroundColor:['#22c55e','#ef4444','#f59e0b'],
      borderWidth:0,
      hoverOffset:4
    }}]
  }},
  options:{{
    responsive:true,
    maintainAspectRatio:false,
    cutout:'72%',
    plugins:{{
      legend:{{display:false}},
      tooltip:{{
        backgroundColor:'#1a2d42',
        titleColor:'#9ab0c4',
        bodyColor:'#e0eaf5',
        borderColor:'#1e3a54',
        borderWidth:1
      }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return html
