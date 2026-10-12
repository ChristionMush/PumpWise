# Billing providers

The payment company is **not chosen yet**. Everything provider-independent is built and tested: pricing, payments, reminders (day 1/3/7), auto-suspension (day 10), instant reactivation, new-subscription signup, reseller share.

To add Stripe, Paystack, Flutterwave or a local provider:
1. Subclass `apps.billing.providers.BillingProvider`; implement `parse_webhook(headers, body)` (verify the vendor's signature exactly as its documentation says, reject stale timestamps) and `create_checkout(...)`.
2. Map the vendor's payload to `BillingEvent`: `id` (the vendor's unique event id: this is the idempotency key), `type` (`payment.succeeded` / `payment.failed`), `amount`, `currency`, `months`, and either `tenant_id` (existing customer) or `customer` (new customer: `business_name`, `owner_name`, `owner_email`/`owner_phone`, `stations` [names], `plan`, `addons`, `reseller`). Put the tenant id or signup details in the vendor's metadata field when creating the checkout.
3. Register it in `settings.BILLING_PROVIDERS` and set `BILLING_DEFAULT_PROVIDER`; the webhook URL is `/api/billing/webhook/<name>/`.
4. Test with the vendor's sandbox: bad signature -> 400; repeated event -> `duplicate`; paid suspended tenant -> reactivated.

`HmacJsonProvider` (name `hmac`) is a complete reference: body JSON, `X-Timestamp` and `X-Signature = HMAC-SHA256(secret, "<ts>.<body>")`. Use it for a bank/aggregator bridge or for manual testing (`apps.billing.providers.sign`).
