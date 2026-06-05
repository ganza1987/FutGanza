import os
import httpx
import logging

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ── Conditions registry ──────────────────────────────────────────────────────
# Each condition has an id, description, and weight (1-10).
# In the future these will be editable per user via /condiciones command.

DEFAULT_CONDITIONS = [
    {"id": "btts",        "label": "Ambos equipos marcan (BTTS)",          "weight": 8},
    {"id": "over25",      "label": "Más de 2.5 goles en el partido",        "weight": 7},
    {"id": "home_form",   "label": "El local tiene mejor forma reciente",   "weight": 6},
    {"id": "away_goals",  "label": "Visitante promedia >1.5 goles/partido", "weight": 5},
    {"id": "clean_sheet", "label": "Al menos un equipo con portería a 0 en últimos 3 partidos", "weight": 4},
    {"id": "home_unbeaten","label": "Local invicto en sus últimos 5",        "weight": 6},
    {"id": "h2h_goals",   "label": "H2H histórico: partidos con goles de ambos", "weight": 5},
]


def build_analysis_prompt(home: str, away: str, conditions: list[dict]) -> str:
    conditions_block = "\n".join(
        f"  - [{c['id']}] {c['label']} (peso {c['weight']}/10)"
        for c in conditions
    )

    return f"""Eres un analista deportivo experto en fútbol. Tu tarea es generar un análisis estadístico completo del enfrentamiento entre *{home}* (local) y *{away}* (visitante).

Usa tu conocimiento actualizado de ambos equipos para la temporada actual o más reciente disponible.

---

## ESTRUCTURA DEL INFORME (obligatoria, usa exactamente estos encabezados)

### 1. 📋 RESUMEN DEL PARTIDO
- Competición y contexto
- Importancia del partido (liga, copa, clasificación)

### 2. 🔵 {home} — Tendencias recientes
- Últimos 5 partidos: resultado, rival, goles marcados/encajados
- Forma: racha actual (victorias/empates/derrotas)
- Promedio de goles marcados y encajados
- Rendimiento en casa (últimas 5 jornadas como local)
- Tendencia estadística destacada (ej: "marca en el primer tiempo en el 80% de sus partidos")

### 3. 🔴 {away} — Tendencias recientes
- Últimos 5 partidos: resultado, rival, goles marcados/encajados
- Forma: racha actual
- Promedio de goles marcados y encajados
- Rendimiento como visitante (últimas 5 como visitante)
- Tendencia estadística destacada

### 4. ⚔️ COMPARATIVA DIRECTA
- Historial H2H reciente (últimos 3-5 enfrentamientos)
- Quién domina el H2H y en qué estadio
- Promedio de goles en sus enfrentamientos directos

### 5. ✅❌ EVALUACIÓN DE CONDICIONES
Para cada condición, indica si SE CUMPLE ✅ o NO SE CUMPLE ❌, con una justificación breve basada en datos:

{conditions_block}

Al final muestra una tabla resumen:
| Condición | Estado | Peso | Puntos |
con los puntos = peso si se cumple, 0 si no.

### 6. 📊 PUNTUACIÓN GLOBAL
- Suma de puntos obtenidos vs puntos máximos posibles
- Porcentaje de cumplimiento: XX%
- Interpretación: FAVORABLE / NEUTRO / DESFAVORABLE para apuesta/predicción

### 7. 🔮 CONCLUSIÓN
- 2-3 líneas de conclusión con la tendencia más relevante
- Mercado o resultado más avalado por los datos

---

IMPORTANTE:
- Sé preciso con los datos; si no tienes certeza absoluta de un resultado, indícalo con "aprox." 
- Usa formato Markdown compatible con Telegram (negrita con *, cursiva con _, listas con -)
- No uses # para encabezados en el output final, usa *TEXTO EN MAYÚSCULAS* o emojis como separadores
- Sé conciso pero completo; el análisis debe caber en ~3000 caracteres
"""


async def analyze_match(
    home: str,
    away: str,
    conditions: list[dict] | None = None
) -> str:
    if conditions is None:
        conditions = DEFAULT_CONDITIONS

    prompt = build_analysis_prompt(home, away, conditions)

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-opus-4-20250514",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "Eres un analista deportivo experto. Respondes siempre en español. "
            "Usas datos reales y actualizados de fútbol. "
            "Tu formato de salida es Markdown compatible con Telegram."
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            return data["content"][0]["text"]
    except httpx.HTTPStatusError as e:
        logger.error(f"Anthropic API error: {e.response.text}")
        return "❌ Error al generar el análisis. Inténtalo de nuevo en unos segundos."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "❌ Error inesperado. Revisa los logs del servidor."
