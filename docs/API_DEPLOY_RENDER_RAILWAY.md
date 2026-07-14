# Deploy API FastAPI

La dashboard Streamlit resta su Streamlit Cloud. Questo deploy serve solo per
la API sempre accesa: webhook Telegram, webhook Stripe, API partner e controlli
di readiness.

## Scelta consigliata

Per PricePilot parti da Render: e piu lineare per un web service FastAPI e
legge `render.yaml` dal repository. Railway e gia predisposto con
`railway.json`, ma puoi usarlo in alternativa.

## Start command

Entrambe le piattaforme devono avviare la API con:

```bash
uvicorn pricepilot.api.server:app --host 0.0.0.0 --port $PORT
```

Health check:

```text
/health
```

## Variabili ambiente minime

Per il primo deploy pubblico usa staging. Protegge le API con chiave, ma non
pretende ancora Stripe, channel manager e provider dati reali.

```env
PRICEPILOT_ENV=staging
PRICEPILOT_API_AUTH_REQUIRED=1
PRICEPILOT_DEFAULT_ACCOUNT_ID=1
APP_BASE_URL=https://pricepilot-anto.streamlit.app
PRICEPILOT_CORS_ORIGINS=https://pricepilot-anto.streamlit.app,http://localhost:8501
SUPABASE_AUTH_REDIRECT_URL=https://pricepilot-anto.streamlit.app
SUPABASE_PASSWORD_RESET_REDIRECT_URL=https://pricepilot-anto.streamlit.app
PRICEPILOT_ALLOW_LOCAL_AUTH=0
PRICEPILOT_ALLOW_PAID_SIGNUP_WITHOUT_CHECKOUT=0
PRICEPILOT_ALLOW_MANUAL_CYCLE=0
```

Segreti da inserire nel pannello della piattaforma, mai nel repository:

```env
SUPABASE_URL=https://pdjqtuvxvanimhkpvqff.supabase.co
SUPABASE_ANON_KEY=your-publishable-or-legacy-anon-key
SUPABASE_SERVICE_ROLE_KEY=optional-server-side-only
PRICEPILOT_API_KEY=generate-a-long-random-secret
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_BOT_USERNAME=Price_PilotBot
TELEGRAM_WEBHOOK_SECRET=generate-a-long-random-secret
```

## Passi Render

1. Apri Render e scegli `New` -> `Web Service`.
2. Collega il repository `Antoimmobiliare/pricepilot`.
3. Branch: `main`.
4. Render puo leggere `render.yaml`; se compili manualmente usa:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn pricepilot.api.server:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
5. Inserisci le variabili ambiente e i segreti.
6. Fai deploy.
7. Copia l'URL pubblico della API, per esempio:

```text
https://pricepilot-api.onrender.com
```

8. Imposta `PRICEPILOT_API_BASE_URL` con quell'URL.
9. Quando vuoi attivare il webhook Telegram reale, imposta:

```env
PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=1
PRICEPILOT_REQUIRE_TELEGRAM_WEBHOOK_SECRET=1
```

Poi fai redeploy o riavvia il servizio.

## Passi Railway

1. Crea un nuovo progetto Railway dal repository GitHub.
2. Seleziona branch `main`.
3. Railway usera `railway.json`; in caso contrario imposta lo start command:

```bash
uvicorn pricepilot.api.server:app --host 0.0.0.0 --port $PORT
```

4. Inserisci le stesse variabili ambiente.
5. Dopo il deploy, copia il dominio pubblico Railway e usalo come
   `PRICEPILOT_API_BASE_URL`.

## Verifica

Dopo il deploy apri:

```text
https://your-api-domain/health
https://your-api-domain/ready
```

In staging `/ready` puo segnalare che Stripe o provider reali non sono ancora
attivi. Questo e normale prima dell'acquisto degli strumenti esterni.

Quando passerai alla vendita reale:

```env
PRICEPILOT_ENV=production
```

e `/ready` dovra tornare `ok=true`.
