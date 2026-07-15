# PricePilot

PricePilot e una piattaforma SaaS di dynamic pricing per affitti brevi.

Analizza mercato, competitor, occupazione, calendario, eventi e guardrail di sicurezza per generare decisioni prezzo motivate. In base al piano scelto puo:

- inviare solo consigli prezzo;
- richiedere approvazione Telegram;
- preparare l'aggiornamento automatico tramite channel manager.

La dashboard e costruita con Streamlit. Il backend locale usa SQLite come fallback di sviluppo, mentre Supabase e il database/auth target per la SaaS reale.

## Stato prodotto

Funzionalita gia presenti:

- landing page SaaS pubblica;
- registrazione, login, reset password e consensi;
- onboarding iniziale proprieta;
- modello account/proprieta con isolamento account;
- pricing engine locale;
- calendario prezzi;
- decision log;
- scheduler e storico run;
- Telegram bot con collegamento proprieta e approvazione/rifiuto;
- API FastAPI con health/readiness e webhook production-ready;
- provider contracts per market data, eventi, occupancy, channel manager e billing;
- provider CSV manuale per test realistici gratuiti prima delle API esterne;
- Supabase schema con RLS;
- Smoobu adapter scaffold;
- Stripe billing provider con checkout, portal e webhook piano.

Funzionalita da collegare per produzione commerciale:

- channel manager reale, partendo da Smoobu;
- dati reali competitor/eventi/occupazione;
- privacy policy e termini definitivi validati legalmente.

## Struttura

```text
pricepilot/
  api/                  FastAPI endpoints e webhook Telegram
  core/                 SQLite, Supabase client, scheduler, piani
  dashboard/            Streamlit landing, auth e dashboard
  engine/               Decision engine e analisi pricing
  integrations/         Adapter OTA/channel manager
  providers/            Contratti e provider esterni
  services/             Account, proprieta, Supabase sync, Telegram
  pricing/              Motore pricing legacy/supporto
  data_sources/         Dati demo competitor/eventi
supabase/
  schema.sql            Schema Supabase e policy RLS
tests/                  Test base SaaS, sicurezza, billing, Smoobu
```

## Setup locale

Da PowerShell:

```powershell
cd "C:\Users\UTENTE\OneDrive\Desktop\ULTIMO PP\PricePilot_v4.1"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Nel file `.env` inserire almeno:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
PRICEPILOT_ENV=development
APP_BASE_URL=http://localhost:8501
PRICEPILOT_API_BASE_URL=http://localhost:8000
PRICEPILOT_CORS_ORIGINS=http://localhost:8501
```

Per avviare la dashboard:

```powershell
.\.venv\Scripts\python.exe -m streamlit run pricepilot/dashboard/app.py --server.port 8501
```

Aprire:

```text
http://localhost:8501
```

## Dati manuali prima delle API

Prima di comprare strumenti esterni puoi testare PricePilot con CSV reali:

```text
PRICEPILOT_DATA_PROVIDER=manual
PRICEPILOT_MANUAL_MARKET_CSV=data/manual_market.csv
PRICEPILOT_MANUAL_EVENTS_CSV=data/manual_events.csv
PRICEPILOT_MANUAL_OCCUPANCY_CSV=data/manual_occupancy.csv
```

I template sono:

- `data/manual_market_sample.csv`;
- `data/manual_events_sample.csv`;
- `data/manual_occupancy_sample.csv`.

Copia i sample, rimuovi `_sample` dal nome file e inserisci dati reali raccolti
manualmente. Il motore usera questi dati senza modificare pricing engine o
dashboard.

## Supabase

1. Creare un progetto Supabase.
2. Aprire SQL Editor.
3. Eseguire `supabase/schema.sql`.
4. Impostare in `.env`:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-or-publishable-key
SUPABASE_AUTH_REDIRECT_URL=http://localhost:8501
SUPABASE_PASSWORD_RESET_REDIRECT_URL=http://localhost:8501
```

Per produzione, usare l'URL pubblico Streamlit/API al posto di localhost.
In Supabase aprire `Authentication -> URL Configuration` e impostare:

- `Site URL`: lo stesso valore di `SUPABASE_AUTH_REDIRECT_URL`;
- `Redirect URLs`: URL locale e URL pubblico dell'app, per esempio `http://localhost:8501` e `https://tuo-dominio.streamlit.app`.

Le tabelle account-scoped sono protette da RLS. PricePilot scrive su Supabase
solo quando esiste una sessione utente Supabase valida oppure quando e
configurata una service role server-side:

```env
SUPABASE_SERVICE_ROLE_KEY=
```

La service role serve per sync controllate, migrazioni e processi backend. Non
deve mai essere esposta nel frontend o condivisa.

### Supabase come database operativo unico

Durante sviluppo PricePilot puo usare SQLite locale. Prima di avviare servizi
separati (Streamlit Cloud, API Render, webhook Telegram e scheduler) esegui il
cutover documentato in [docs/SUPABASE_CLOUD_PRIMARY_CUTOVER.md](docs/SUPABASE_CLOUD_PRIMARY_CUTOVER.md).
Alla fine dello step le applicazioni ricevono soltanto:

```env
PRICEPILOT_DATABASE_BACKEND=supabase
SUPABASE_SERVICE_ROLE_KEY=server-side-only-secret
```

Da quel momento SQLite non viene piu usato come fallback: un errore cloud ferma
l'operazione, evitando dati diversi tra dashboard, API e Telegram.

Per deploy pubblico impostare `PRICEPILOT_ENV=production`. In produzione
`PRICEPILOT_AUTH_MODE=disabled` viene bloccato e il fallback locale resta
disattivato salvo override esplicito per test interni.

## Telegram bot

1. Creare il bot con BotFather.
2. Salvare token e username in `.env`.

```env
TELEGRAM_BOT_TOKEN=123456:token
TELEGRAM_BOT_USERNAME=Price_PilotBot
```

Per test locale in polling:

```powershell
.\.venv\Scripts\python.exe -m pricepilot.services.telegram_bot
```

Per produzione e preferibile il webhook tramite FastAPI. Usa l'URL pubblico
dell'API, non quello della dashboard Streamlit:

```env
PRICEPILOT_API_BASE_URL=https://your-api-domain.com
TELEGRAM_WEBHOOK_SECRET=strong-secret
PRICEPILOT_REQUIRE_TELEGRAM_WEBHOOK_SECRET=1
PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=1
```

Con `PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=1`, all'avvio dell'API
PricePilot registra il webhook Telegram verso:

```text
https://your-api-domain.com/telegram/webhook
```

Lascia `PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=0` finche stai usando il
polling locale o finche l'API pubblica non e online.

## API

Avvio locale:

```powershell
.\.venv\Scripts\python.exe -m uvicorn pricepilot.api.server:app --host 0.0.0.0 --port 8000
```

In produzione proteggere le API con:

```env
PRICEPILOT_ENV=production
PRICEPILOT_API_BASE_URL=https://your-api-domain.com
PRICEPILOT_API_KEYS_JSON={"api-key-account-1":1}
PRICEPILOT_CORS_ORIGINS=https://your-dashboard-domain.streamlit.app
```

Endpoint di controllo:

- `GET /health`: conferma che il servizio risponde;
- `GET /ready`: verifica configurazione produzione, API key, Supabase,
  Telegram webhook secret e Stripe webhook secret.

`/ready` resta pubblico per permettere controlli di deploy/monitoring, ma le
API private sono bloccate da API key in produzione.

Per deploy FastAPI su Render o Railway sono inclusi:

- `render.yaml`;
- `railway.json`;
- guida `docs/API_DEPLOY_RENDER_RAILWAY.md`.

## Billing Stripe

Il provider Stripe gestisce checkout, Customer Portal e webhook di aggiornamento piano.

Variabili principali:

```env
STRIPE_SECRET_KEY=
STRIPE_PRICE_PLUS=
STRIPE_PRICE_PRO=
STRIPE_SUCCESS_URL=https://your-domain.streamlit.app?billing=success
STRIPE_CANCEL_URL=https://your-domain.streamlit.app?billing=cancel
STRIPE_PORTAL_RETURN_URL=https://your-domain.streamlit.app
STRIPE_WEBHOOK_SECRET=
```

Nel dashboard Stripe configurare un webhook verso:

```text
https://your-api-domain.com/stripe/webhook
```

Eventi minimi:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`

Solo il webhook Stripe aggiorna `plan`, `billing_status`, `stripe_customer_id`
e `stripe_subscription_id`. La modifica manuale del piano da dashboard/API
utente e bloccata.

## Smoobu

Adapter live predisposto in `pricepilot/integrations/smoobu.py`.
Il flusso supportato e:

```text
Decisione PricePilot -> approvazione Telegram -> POST /api/rates Smoobu -> sync OTA Smoobu
```

Variabili:

```env
SMOOBU_API_CONSUMER_KEY=
SMOOBU_API_CONSUMER_SECRET=
SMOOBU_APARTMENT_ID=
SMOOBU_API_BASE=https://login.smoobu.com
```

`SMOOBU_APARTMENT_ID` deve corrispondere al `listing_id` della proprieta
PricePilot. Se la proprieta ha `platform=smoobu`, piano Plus/Pro e sync mode
approval/auto, PricePilot puo applicare il prezzo tramite Smoobu quando
l'adapter risulta live.

In dashboard, tab `Integrazioni`, il pulsante `Test Smoobu` legge la lista
appartamenti e conferma che le credenziali funzionano. Il polling Telegram puo
restare locale durante i test; in produzione usare webhook API.

## Test

```powershell
.\.venv\Scripts\python.exe -m compileall -f pricepilot tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Regole repository

Non committare:

- `.env`;
- `.venv/`;
- database locali `*.db`;
- log Streamlit;
- zip export;
- cache Python.

Il file `.gitignore` contiene gia queste esclusioni. Il database locale puo restare sul PC, ma non deve essere tracciato da Git.

## Deploy note

Per Streamlit Cloud impostare le variabili in Secrets/Environment, non in `.env`.

Per una SaaS reale servono almeno due deploy separati:

- dashboard Streamlit;
- API FastAPI sempre online per webhook Telegram, billing webhook e integrazioni channel manager.

Checklist operative:

- `docs/DEPLOYMENT.md`;
- `docs/STREAMLIT_CLOUD.md`;
- `.streamlit/secrets.toml.example`;
- `docs/STRIPE_TEST_CHECKLIST.md`;
- `docs/PRE_TOOL_ROADMAP.md`.
