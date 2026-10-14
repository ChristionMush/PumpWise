# Offline entry and sync: the rules

The phone saves every entry locally first (IndexedDB), shows "All saved / N waiting / Offline", and sends the queue when signal returns (and every 30 s while online).

1. **Idempotent:** every entry has a phone-generated `op_id`. Resending is always safe; the server returns the stored result and changes nothing. Dips and deliveries also carry their own `client_id`, so even a rebuilt queue cannot record the same dip twice.
2. **One batch per shift:** the phone sends the leading run of queued entries that belong to one shift in a single call (`POST /api/sync/`) and gets a result per entry. Entries apply in the order they were made. The shift id is created on the phone, so later entries can refer to it before the server has seen the shift.
3. **Never blocks on odd numbers:** meter rollover is handled; backwards readings, gaps between shifts and missing prices are saved and **flagged** for a manager, not refused.
4. **Never overwrites:** start/end meter numbers are per nozzle per shift, entered once by anyone on it.
   * The same number again (from anyone) returns the row already saved.
   * The same person sending a different number is a correction: the old row is kept (soft-deleted), the new one is marked `corrected`, and the shift's sales are rebuilt.
   * A different person's different number is kept beside the first and both are flagged **readings differ**. The first number counts until the lead or a manager picks the right one (`POST /api/shifts/<id>/readings/resolve/`).
5. **Several people, one shift:** each person hands in their own cash (`shift_cash`, or the cash in their `shift_end`). The lead's `shift_end` asks to close; the shift closes by itself once everyone on it has handed in or been marked as left. So two phones can sync in either order.
6. **Two phones that both started offline:** on a several-attendants station, a second `shift_start` while a shift is open joins that shift instead of opening another. Its result says `merged` and gives `client_shift_id`; the phone may keep using the id it made up, which the server resolves to the merged shift.
7. **Reviewed shifts are final:** entries for them are rejected, kept in the sync log, and shown to managers (`GET /api/sync/rejected/`).
8. **Roles:** attendants cannot sync dips or deliveries (rule 11) or change another person's entries; the lead and managers can close, mark someone as left, reassign pumps and resolve readings.
9. **Isolation:** every referenced id is resolved through Row-Level Security, so an entry can never touch another business's records; a shift id already used elsewhere is refused without revealing it.
10. **Price at the right time:** a sale uses the price in force when its period began. A mid-shift (checkpoint) reading splits a nozzle's period: used when the price changes, and automatically when a pump is handed to someone else on an assigned-pump station.

Entry kinds: `shift_start`, `shift_join`, `shift_leave`, `shift_cash`, `shift_end`, `shift_close`, `checkpoint`, `pump_reassign` (attendants and up); `tank_dip`, `delivery` (managers and owners only).
