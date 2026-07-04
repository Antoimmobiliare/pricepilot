# Stripe test checklist

Use Stripe test mode before enabling live payments.

## Required env vars

```text
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PRICE_PLUS=price_...
STRIPE_PRICE_PRO=price_...
STRIPE_SUCCESS_URL=https://your-dashboard-domain?billing=success
STRIPE_CANCEL_URL=https://your-dashboard-domain?billing=cancel
STRIPE_PORTAL_RETURN_URL=https://your-dashboard-domain
STRIPE_WEBHOOK_SECRET=whsec_...
```

## Flow

1. Register a new user from the landing page.
2. Start from Free.
3. Click upgrade to Plus.
4. Complete Stripe test checkout.
5. Send a `checkout.session.completed` webhook.
6. Verify account plan becomes Plus and `billing_status=active`.
7. Open customer portal.
8. Cancel subscription in Stripe test mode.
9. Send `customer.subscription.deleted`.
10. Verify account plan returns to Free.

## Expected behavior

- Paid plans are not granted in production before checkout.
- Stripe webhook without signature is rejected in production.
- Customer portal is available only after a customer id exists.
- Account profile update cannot manually overwrite billing fields.
