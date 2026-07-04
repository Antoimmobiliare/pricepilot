# PricePilot deployment checklist

This checklist closes the free pre-tool phase. Use it before buying channel
manager, market-data, or event APIs.

## Dashboard

Run command:

```powershell
.\.venv\Scripts\python.exe -m streamlit run pricepilot/dashboard/app.py --server.port 8501
```

Production environment:

- `PRICEPILOT_ENV=production`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `APP_BASE_URL=https://your-dashboard-domain`
- `SUPABASE_AUTH_REDIRECT_URL=https://your-dashboard-domain`
- `SUPABASE_PASSWORD_RESET_REDIRECT_URL=https://your-dashboard-domain`
- `PRICEPILOT_ALLOW_LOCAL_AUTH=0`
- `PRICEPILOT_ALLOW_PAID_SIGNUP_WITHOUT_CHECKOUT=0`
- `PRICEPILOT_ALLOW_MANUAL_CYCLE=0`

## API

Run command:

```powershell
.\.venv\Scripts\python.exe -m uvicorn pricepilot.api.server:app --host 0.0.0.0 --port 8000
```

Production environment:

- `PRICEPILOT_ENV=production`
- `PRICEPILOT_API_BASE_URL=https://your-api-domain`
- `PRICEPILOT_API_KEYS_JSON={"customer-api-key":1}`
- `TELEGRAM_WEBHOOK_SECRET`
- `PRICEPILOT_REQUIRE_TELEGRAM_WEBHOOK_SECRET=1`
- `PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=1`
- `STRIPE_WEBHOOK_SECRET`

Check API readiness:

```text
GET https://your-api-domain/ready
```

The response must return `ok=true` before public testing.

## Free data mode

Before buying APIs, use CSV data:

```text
PRICEPILOT_DATA_PROVIDER=manual
PRICEPILOT_MANUAL_MARKET_CSV=data/manual_market.csv
PRICEPILOT_MANUAL_EVENTS_CSV=data/manual_events.csv
PRICEPILOT_MANUAL_OCCUPANCY_CSV=data/manual_occupancy.csv
```

Copy the sample files in `data/` and rename them without `_sample`.

## Go/no-go

- Supabase RLS enabled and schema applied.
- New user can register and confirm email.
- Terms and Privacy are visible from signup.
- User can create one property.
- Telegram can link to the property.
- Free sends recommendations only.
- Plus sends approval and stores decision state.
- Pro only auto-applies when guardrails pass.
- Stripe test checkout upgrades/downgrades the account.
- `/ready` is green in production.
