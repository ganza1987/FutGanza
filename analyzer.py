import os
import httpx
import logging

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

DEFAULT_CONDITIONS = [
    {"id": "btts",         "label": "Ambos equipos marcan (BTTS)",                     "weight": 8},
    {"id": "over25",       "label": "Más de 2.5 goles en el partido",                   "weight": 7},
    {"id": "home_form",    "label": "El local tiene mejor forma reciente",              "weight": 6},
    {"id": "away_goals",   "label": "Visitante promedia >1.5 goles/partido",            "weight": 5},
    {"id": "clean_sheet",  "label": "Al menos un equipo con portería a 0 en últimos 3", "weight": 4},
    {"id": "home_unbeaten","label": "Local invicto en sus últimos 5",                   "weight": 6},
    {"id": "h2h_goals",    "label": "H2H histórico: partidos con goles de ambos",       "weight": 5},
]


def build_prompt(home: str, away: str, conditions: list[dict]) -> str:
    conditions_block = "\n".join(
        f"  - [{c['id']}] {c['label']} (peso {c['weight']}/10)"
        for c in conditions
    )
    return f"""Eres un analista deportivo experto. Debes generar un análisis REAL y ACTUALIZADO del partido entre *{home}* (local) y *{away}* (visitante).

PASO 1 — BÚSQUEDA OBLIGATORIA:
Antes de escribir el análisis, usa la herramienta web_search para buscar:
1. "{home} últimos partidos resultados 2025 2026"
2. "{away} últimos partidos resultados 2025 2026"  
3. "{home} vs {away} historial head to head"

PASO 2 — ANÁLISIS con los datos encontrados:
Usa ÚNICAMENTE los datos reales encontrados en las búsquedas. Si no encuentras un dato concreto, escribe "sin dato disponible" en lugar de inventarlo.

ESTRUCTURA OBLIGATORIA:

*⚽ {home.upper()} vs {away.upper()}*

*📋 1. CONTEXTO*
- Competición y contexto actual

*🔵 2. {home.upper()} — TENDENCIAS REALES*
- Últimos 5 partidos con fechas y marcadores reales
- Forma: W/D/L de los últimos 5
- Promedio goles marcados / encajados
- Tendencia estadística destacada

*🔴 3. {away.upper()} — TENDENCIAS REALES*
- Últimos 5 partidos con fechas y marcadores reales
- Forma: W/D/L de los últimos 5
- Promedio goles marcados / encajados
- Tendencia estadística destacada

*⚔️ 4. H2H DIRECTO*
- Últimos enfrentamientos directos reales
- Dominio histórico y goles medios

*✅ 5. EVALUACIÓN DE CONDICIONES*
Para cada condición indica ✅ SE CUMPLE o ❌ NO SE CUMPLE con justificación basada en datos reales buscados:

{conditions_block}

Tabla resumen:
Condición | ✅/❌ | Peso | Pts

*📊 6. PUNTUACIÓN GLOBAL*
- Puntos obtenidos / máximos posibles
- Porcentaje: XX%
- FAVORABLE / NEUTRO / DESFAVORABLE

*🔮 7. CONCLUSIÓN*
- Tendencia principal avalada por datos reales
- Mercado más respaldado

Formato: Markdown Telegram (negrita *, cursiva _). Máximo 3500 caracteres.
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

    # Use web_search tool so Claude can fetch real data
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4000,
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 5
            }
        ],
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto en fútbol. Respondes siempre en español. "
            "SIEMPRE usas la herramienta web_search para buscar datos reales y actualizados antes de responder. "
            "NUNCA inventas resultados o estadísticas. Si no encuentras un dato, lo indicas claramente. "
            "Tu formato de salida es Markdown compatible con Telegram."
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            # Extract text from response (may contain tool_use blocks)
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
