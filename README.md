# ⚽ Football Analysis Bot

Bot de Telegram que genera análisis estadísticos completos de partidos de fútbol usando Claude AI.

## Características

- 📨 **Análisis bajo demanda** — escribe `Real Madrid vs Barcelona` y recibes el informe
- 🔔 **Análisis automáticos** — se envían X horas antes del partido (configurable)
- ✅❌ **Evaluación de condiciones** — estadísticas ponderadas que se cumplen o no
- 📊 **Puntuación global** — porcentaje de cumplimiento de las condiciones
- 🚀 **Desplegable en Railway/Render** — servidor siempre activo, gratis

---

## Paso 1: Crear el bot de Telegram

1. Abre Telegram y busca **@BotFather**
2. Escribe `/newbot`
3. Ponle un nombre (ej: `Football Analyzer`)
4. Ponle un username que acabe en `bot` (ej: `mi_football_analyzer_bot`)
5. BotFather te dará el **TOKEN** — guárdalo, lo necesitarás

Para obtener tu **chat_id** personal:
1. Busca **@userinfobot** en Telegram
2. Escríbele cualquier cosa
3. Te responderá con tu ID numérico

---

## Paso 2: Desplegar en Railway (gratis)

### Opción A — Desde GitHub (recomendado)

1. Sube esta carpeta a un repositorio GitHub
2. Ve a [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
3. Selecciona tu repositorio
4. Railway detecta el `Procfile` automáticamente

### Opción B — Railway CLI

```bash
npm install -g @railway/cli
railway login
railway init
railway up
```

### Configurar variables de entorno en Railway

En el panel de Railway → tu proyecto → **Variables**, añade:

| Variable | Valor |
|----------|-------|
| `TELEGRAM_TOKEN` | El token de @BotFather |
| `WEBHOOK_URL` | La URL pública que Railway te asigna (ej: `https://tu-app.up.railway.app`) |
| `ANTHROPIC_API_KEY` | Tu clave de [console.anthropic.com](https://console.anthropic.com) |
| `NOTIFY_CHAT_IDS` | Tu chat_id (o varios separados por comas) |
| `ALERT_HOURS` | `2` (análisis 2h antes del partido) |

---

## Paso 3: Probar el bot

1. Abre Telegram → busca tu bot por su username
2. Escribe `/start`
3. Escribe un partido: `Real Madrid vs Barcelona`
4. En ~10 segundos recibirás el análisis completo

---

## Añadir partidos para análisis automático

Edita `fixtures.json` con los partidos que quieras monitorizar:

```json
[
  {
    "home": "Real Madrid",
    "away": "Barcelona",
    "kickoff": "2025-10-26T19:00:00Z"
  }
]
```

El bot comprobará cada hora si algún partido empieza en las próximas `ALERT_HOURS` horas y enviará el análisis automáticamente.

---

## Estructura del análisis generado

Cada análisis incluye:
1. **Resumen del partido** — contexto y competición
2. **Tendencias del local** — forma reciente, goles, estadísticas
3. **Tendencias del visitante** — ídem
4. **Comparativa H2H** — historial directo
5. **Evaluación de condiciones** — cada condición ✅ o ❌ con su peso
6. **Puntuación global** — % de cumplimiento
7. **Conclusión** — mercado más avalado por los datos

---

## Personalizar condiciones (próximamente vía /condiciones)

Edita `analyzer.py` → `DEFAULT_CONDITIONS` para cambiar las condiciones y sus pesos:

```python
DEFAULT_CONDITIONS = [
    {"id": "btts",    "label": "Ambos equipos marcan",     "weight": 8},
    {"id": "over25",  "label": "Más de 2.5 goles",         "weight": 7},
    # Añade las tuyas aquí...
]
```

---

## Archivos del proyecto

```
football-bot/
├── main.py           # FastAPI app + webhook
├── bot_handler.py    # Procesamiento de mensajes Telegram
├── analyzer.py       # Llamada a Claude API + condiciones
├── scheduler.py      # Análisis automáticos pre-partido
├── fixtures.json     # Partidos programados
├── requirements.txt  # Dependencias Python
├── Procfile          # Comando de inicio (Railway/Render)
├── railway.toml      # Configuración Railway
└── .env.example      # Plantilla de variables de entorno
```
