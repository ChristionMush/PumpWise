# Offline entry and sync: the rules

The phone saves every entry locally first (IndexedDB), shows "All saved / N waiting / Offline", and sends the queue when signal returns (and every 30 s while online).

1. **Idempotent:** every entry has a phone-generated `op_id`. Resending is always safe; the server returns the stored result and changes nothing.
2. **Ordered:** entries apply in the order they were made. The shift id is created on the phone, so later entries can refer to it before the server has seen the shift.
3. **Never blocks on odd numbers:** meter rollover is handled; backwards readings, gaps between shifts and missing prices are saved and **flagged** for a manager, not refused.
4. **Corrections:** re-sending an end reading for an unreviewed shift replaces the number (marked `corrected`; the audit log keeps history) and rebuilds that shift's sales. Old sales rows are soft-deleted, never destroyed.
5. **Reviewed shifts are final:** entries for them are rejected, kept in the sync log, and shown to managers (`GET /api/sync/rejected/`).
6. **Two people, same pump:** each shift has its own readings; the next shift's start number is compared with the previous end number and any difference is flagged as a gap. Attendants cannot change another attendant's shift; managers and owners can.
7. **Isolation:** every referenced id is resolved through Row-Level Security, so an entry can never touch another business's records; a shift id already used elsewhere is refused without revealing it.
8. **Price at the right time:** a sale uses the price in force when its period began. If the price changes during a shift, a mid-shift (checkpoint) reading splits the shift so each part gets the right price; without one the whole nozzle period uses the price at the start of the period.
