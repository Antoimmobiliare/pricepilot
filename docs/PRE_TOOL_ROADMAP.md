# Pre-tool closure roadmap

These items must be green before buying external tools.

1. Repository clean: no local db, logs, zip exports, or secrets tracked.
2. Supabase production mode: Auth, RLS, accounts, properties, pricing rules, and consents working.
3. UI copy: no internal/demo language shown to normal users.
4. Stripe test mode: checkout, webhook, portal, downgrade.
5. Manual CSV data provider: realistic competitor, event, and occupancy data without paid APIs.
6. Deploy package: dashboard, API, `/ready`, webhook secrets, and CI tests.

After this phase, the remaining paid work is channel manager credentials,
market/event data APIs, legal validation, and live billing.
