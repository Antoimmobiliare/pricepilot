# PricePilot Streamlit Cloud Deploy

Questa guida serve per pubblicare la dashboard PricePilot su Streamlit Cloud.
La dashboard Streamlit gestisce landing, login, onboarding e area utente.
Webhook Telegram, Stripe e automazioni server-side richiedono anche un deploy API
FastAPI separato.

## 1. Prima del deploy

Verifiche locali:

```powershell
.\.venv\Scripts\python.exe -m compileall -f pricepilot tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git status --short
```

Lo stato Git deve essere pulito o contenere solo modifiche che vuoi pubblicare.

## 2. GitHub

Repository:

```text
https://github.com/Antoimmobiliare/pricepilot.git
```

Branch consigliato:

```text
main
```

File principale Streamlit:

```text
pricepilot/dashboard/app.py
```

Python runtime:

```text
runtime.txt -> python-3.12
```

Dipendenze:

```text
requirements.txt
```

## 3. Streamlit Cloud

1. Apri Streamlit Cloud.
2. Crea una nuova app.
3. Seleziona repository `Antoimmobiliare/pricepilot`.
4. Branch: `main`.
5. Main file path: `pricepilot/dashboard/app.py`.
6. In Advanced settings incolla i secrets.

Template secrets:

```text
.streamlit/secrets.toml.example
```

Non committare mai `.streamlit/secrets.toml` reale.

## 4. Secrets minimi dashboard

Sostituisci `your-app` e `your-project` con i valori reali:

```toml
PRICEPILOT_ENV = "production"
PRICEPILOT_DATA_PROVIDER = "demo"

SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_ANON_KEY = "your-supabase-publishable-or-anon-key"

APP_BASE_URL = "https://your-app.streamlit.app"
SUPABASE_AUTH_REDIRECT_URL = "https://your-app.streamlit.app"
SUPABASE_PASSWORD_RESET_REDIRECT_URL = "https://your-app.streamlit.app"
PRICEPILOT_CORS_ORIGINS = "https://your-app.streamlit.app"

PRICEPILOT_ALLOW_LOCAL_AUTH = "0"
PRICEPILOT_ALLOW_PAID_SIGNUP_WITHOUT_CHECKOUT = "0"
PRICEPILOT_ALLOW_MANUAL_CYCLE = "0"
PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK = "0"
```

Telegram puo essere testato dalla dashboard se aggiungi:

```toml
TELEGRAM_BOT_TOKEN = "123456:token"
TELEGRAM_BOT_USERNAME = "Price_PilotBot"
```

Con solo Streamlit Cloud non usare webhook Telegram: lascia
`PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK = "0"`.

## 5. Supabase redirect

In Supabase:

```text
Authentication -> URL Configuration
```

Imposta:

```text
Site URL: https://your-app.streamlit.app
Redirect URLs:
  http://localhost:8501
  https://your-app.streamlit.app
```

Per test locale e produzione puoi tenere entrambi gli URL.

## 6. Test post-deploy

1. Apri la landing online.
2. Clicca `Inizia Gratis`.
3. Registra un account con email reale.
4. Conferma email.
5. Accedi alla dashboard.
6. Crea una proprieta.
7. Controlla Supabase:
   - `auth.users` ha 1 utente confermato;
   - `profiles` ha 1 riga;
   - `accounts` ha 1 riga;
   - `account_members` ha 1 riga;
   - `properties` ha la proprieta creata;
   - `user_consents` ha Termini/Privacy versione corrente.
8. Crea un secondo account e verifica che non veda i dati del primo.

## 7. Limiti del deploy solo Streamlit

Il deploy Streamlit e sufficiente per:

- landing;
- registrazione/login;
- onboarding;
- dashboard;
- Supabase Auth e database;
- test consigli prezzo;
- Telegram in polling locale o invio test se token configurato.

Serve un deploy API separato per:

- webhook Telegram pubblico;
- webhook Stripe;
- API tenant-protected;
- automazioni server-side sempre accese;
- channel manager live.
