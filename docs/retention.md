# Data retention and deletion policy

Principles: business data is never hard-deleted by the application; customers can take their data at any time; nothing is destroyed because a payment is late.

| Data | Rule |
|---|---|
| Business records (shifts, readings, sales, deliveries, prices, users, audit log) | Kept while the tenant exists. "Deleting" in the app is a soft delete (`deleted_at`); the row stays and appears in exports. The runtime database role has no DELETE/TRUNCATE permission. |
| Suspended tenant (unpaid) | Locked, not deleted. All data retained **at least 90 days** after suspension (`Subscription.suspended_at`), normally until the customer pays or cancels. Attendants can keep recording during the grace period so records have no gaps. |
| Cancelled tenant | Data retained 90 days after cancellation so the owner can export it. After that, deletion happens only by a documented, audited manual process approved by Tendeka & Elichi, after a final export is offered to the customer. |
| Database backups | 14 days (staging) / 35 days (production), point-in-time restore, encrypted, replicated to a second region. |
| Monthly export copies (S3, versioned) | Current version kept; older versions removed after about 13 months. |
| Customer-requested export files | Link valid 24 hours; the file expires and is removed after 30 days (`EXPORT_FILE_DAYS`). The request record stays. |
| Photos (meters, dips, nameplates, logos) | Private per tenant; kept with the record they belong to. |
| Audit log | Append-only (the app role cannot update or delete it). Kept for the life of the tenant. |
| Passwords | Only salted hashes are stored and they are never exported. Temporary passwords are shown once and not stored in clear. |

Right to a copy: Settings, "Export my data" (CSV and JSON).
Right to deletion: handled as a manual, audited process (see above) because a mistaken permanent deletion cannot be undone; it is never automatic.
