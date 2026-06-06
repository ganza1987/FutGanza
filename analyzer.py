import os
import httpx
import logging

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

DEFAULT_CONDITIONS = [
    {"id": "btts",           "label": "Ambos equipos marcan (BTTS)",                         "weight": 8},
    {"id": "over25",         "label": "Más de 2.5 goles en el partido",                       "weight": 7},
    {"id": "home_form",      "label": "El local tiene mejor forma reciente",                  "weight": 6},
    {"id": "away_goals",     "label": "Visitante promedia >1.5 goles/partido",                "weight": 5},
    {"id": "clean_sheet",    "label": "Al menos un equipo con portería a 0 en últimos 3",     "weight": 4},
    {"id": "home_unbeaten",  "label": "Local invicto en sus últimos 5",                       "weight": 6},
    {"id": "h2h_goals",      "label": "H2H histórico: partidos con goles de ambos",           "weight": 5},
    {"id": "over85corners",  "label": "Más de 8.5 corners totales en el partido",             "weight": 6},
    {"id": "home_corners",   "label": "Local promedia más de 5 corners por partido en casa",  "weight": 5},
    {"id": "away_corners",   "label": "Visitante promedia más de 4 corners fuera de casa",    "weight": 4},
    {"id": "shots_home",     "label": "Local promedia más de 5 disparos a puerta en casa",    "weight": 5},
    {"id": "shots_away",     "label": "Visitante promedia más de 4 disparos a puerta fuera",  "weight": 4},
    {"id": "cards_over25",   "label": "Más de 2.5 tarjetas totales en el partido",            "weight": 5},
    {"id": "cards_home",     "label": "Local promedia más de 1.5 tarjetas por partido en casa","weight": 4},
    {"id": "cards_away",     "label": "Visitante promedia más de 2 tarjetas por partido fuera","weight": 4},
    {"id": "cards_h2h",      "label": "H2H histórico con más de 2 tarjetas por partido",      "weight": 3},
]


def build_prompt(home: str, away: str, conditions: list[dict]) -> str:
    conditions_block = "\n".join(
        f"  - [{c['id']}] {c['label']} (peso {c['weight']}/10)"
        for c in conditions
    )
    return f"""Eres un analista deportivo experto en fútbol. Genera un análisis REAL y ACTUALIZADO del partido entre *{home}* (local) y *{away}* (visitante).

PASO 1 — BÚSQUEDAS OBLIGATORIAS (realiza TODAS antes de escribir):
1. "{home} últimos partidos resultados 2025 2026"
2. "{away} últimos partidos resultados 2025 2026"
3. "{home} vs {away} historial head to head"
4. "{home} corners disparos tarjetas estadísticas 2025 2026"
5. "{away} corners disparos tarjetas estadísticas 2025 2026"

PASO 2 — ANÁLISIS con los datos encontrados:
Usa ÚNICAMENTE datos reales de las búsquedas. Si no encuentras un dato concreto, escribe "sin dato disponible".

ESTRUCTURA OBLIGATORIA:

*⚽ {home.upper()} vs {away.upper()}*

*📋 1. CONTEXTO*
- Competición y contexto actual

*🔵 2. {home.upper()} — TENDENCIAS EN CASA*
- Últimos 5 partidos con fechas y marcadores reales
- Forma: W/D/L últimos 5
- ⚽ Goles: promedio marcados / encajados en casa
- 📐 Corners: promedio en casa (si disponible)
- 🎯 Disparos a puerta: promedio en casa (si disponible)
- 🟨 Tarjetas: promedio amarillas y rojas en casa (si disponible)
- Tendencia destacada

*🔴 3. {away.upper()} — TENDENCIAS FUERA*
- Últimos 5 partidos con fechas y marcadores reales
- Forma: W/D/L últimos 5
- ⚽ Goles: promedio marcados / encajados fuera
- 📐 Corners: promedio fuera de casa (si disponible)
- 🎯 Disparos a puerta: promedio fuera de casa (si disponible)
- 🟨 Tarjetas: promedio amarillas y rojas fuera (si disponible)
- Tendencia destacada

*⚔️ 4. H2H DIRECTO*
- Últimos enfrentamientos con marcadores reales
- Dominio histórico, goles medios, corners y tarjetas medias si disponible

*✅ 5. EVALUACIÓN DE CONDICIONES*
Para cada condición indica ✅ SE CUMPLE o ❌ NO SE CUMPLE con justificación basada en datos reales:

{conditions_block}

Tabla resumen:
Condición | ✅/❌ | Peso | Pts

*📊 6. PUNTUACIÓN GLOBAL*
- Puntos obtenidos / máximos posibles
- Porcentaje: XX%
- FAVORABLE / NEUTRO / DESFAVORABLE

*🔮 7. CONCLUSIÓN*
- Tendencia principal avalada por datos reales
- Mercados más respaldados (goles, corners, disparos, tarjetas)

Formato: Markdown Telegram (negrita *, cursiva _). Máximo 4000 caracteres.
Al final añade: 📡 _Fuente: búsqueda web en tiempo real_
"""


async def analyze_match(
    home: str,
    away: str,
    conditions: list[dict] | None = None
) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    prompt = build_prompt(home, away, conditions)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 5000,
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8
            }
        ],
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en fútbol. Respondes siempre en español. "
            "SIEMPRE usas web_search para buscar datos reales antes de responder. "
            "NUNCA inventas resultados, corners, disparos ni tarjetas. Si no encuentras un dato, lo indicas claramente. "
            "Tu formato de salida es Markdown compatible con Telegram."
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            text_parts = [
                block["text"]
                for block in data.get("content", [])
                if block.get("type") == "text"
            ]
            return "\n".join(text_parts) if text_parts else "❌ No se pudo generar el análisis."
    except httpx.HTTPStatusError as e:
        logger.error(f"Anthropic API error: {e.response.text}")
        return "❌ Error al generar el análisis. Inténtalo de nuevo en unos segundos."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "❌ Error inesperado. Revisa los logs del servidor."
