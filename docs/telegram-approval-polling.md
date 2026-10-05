# Telegram approval polling

PricePilot receives Telegram approvals through a bounded GitHub Actions poller.
The worker calls `getUpdates`, passes each valid update to the same application
handler used by the HTTP webhook, persists the Telegram offset in Supabase and
exits. It never calls the Beds24 writer directly.

## Required GitHub configuration

Repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `BEDS24_LUMA_TOKEN`

Repository variable:

- `PRICEPILOT_ALLOW_CHANNEL_WRITES` (`0` is the safe default; set to `1` only
  when approval writes are intentionally enabled)

The pricing scheduler variable remains separate:
`PRICEPILOT_SCHEDULER_ENABLED` is not changed by this workflow.

## Webhook to polling migration

Telegram does not allow `getUpdates` while a webhook URL is active. Apply
`supabase/telegram_update_cursor.sql`, then run once in a trusted server-side
environment:

```text
python -m pricepilot.services.telegram_poller --migrate-from-webhook --verify-only
```

This invokes `deleteWebhook` with `drop_pending_updates=false`, verifies that
the webhook URL is empty and preserves queued Telegram updates. Also keep
`PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=0` on Render so a later restart does
not recreate the conflict.

The scheduled workflow runs at minutes 2, 7, 12, ... 57 of every hour. Each
invocation processes at most 50 updates and terminates. GitHub concurrency is
configured with `cancel-in-progress: false`.

## Cursor and failure semantics

`public.telegram_update_cursor.next_update_id` is the next Telegram update that
the worker may consume. The cursor advances only after the shared handler has
returned successfully. If processing or the database fails, the cursor stays
unchanged and the update is retried. Existing decision-level atomic claims make
re-delivery harmless for price writes.

Structurally malformed or unsupported updates are terminal poison entries:
they are recorded in the cursor status/error and consumed without entering the
approval handler. A processing failure stops the batch, so later updates cannot
overtake an unresolved approval.

## Restoring the webhook

Pause/disable the GitHub polling workflow first. Then configure the Render API
with `PRICEPILOT_AUTO_REGISTER_TELEGRAM_WEBHOOK=1`, a valid
`TELEGRAM_WEBHOOK_SECRET` and `PRICEPILOT_API_BASE_URL`, and redeploy/restart the
API. Verify `getWebhookInfo` before re-enabling approval operations. Never run
webhook delivery and polling at the same time.
