# Tier 2 (automatic data): ingestion design note

Status: NOT implemented. Only the seam exists: `apps/operations/adapters.py` (`DataSourceAdapter`) and the `data_source` field on pumps (`manual`, `pulse_reader`, `controller`) and tanks (`manual`, `atg`). No pump brand support is claimed; pump models will be researched after the system is running (pumps already capture optional brand, model, year, nameplate and display photos for that research).

## What an adapter does
Converts any device feed into the same internal records the manual flow creates: `PumpMeterReading`, `TankReading`, `Transaction`. Reconciliation, reports, alerts and the dashboard do not change.
Register with `adapters.register("pulse_reader", MyAdapter())`. A pump or tank whose `data_source` has no adapter refuses manual entry with a clear message.

## Planned ingestion endpoint (design only)
`POST /api/ingest/<station_id>/readings/` from a device or a small on-site gateway.
- **Authentication:** one device token per station (random, hashed at rest, shown once, revocable, rotated on request). Sent as `Authorization: Device <token>`. The token resolves to exactly one tenant and station and sets the tenant database context, so Row-Level Security applies exactly as for users.
- **Idempotent writes:** every reading carries a device-generated `reading_id` (or `device_id` + monotonic `sequence`); the server stores it with a unique constraint per tenant and returns the stored result for repeats (same mechanism as `SyncOperation`).
- **Batching and offline buffering:** the gateway buffers during outages and sends in order with original timestamps (`reading_at`), up to a few hundred per request.
- **Validation:** reject readings for nozzles/tanks outside the token's station; flag (never drop) odd values: rollover, backwards, gaps, as the manual flow does.
- **Clock drift:** store both device time and server receive time; use device time for pricing, flag drift over a threshold.
- **Rate limits and monitoring:** per-token limits; alert when a station's feed goes silent beyond a configurable window (reuses the missed-reading alert).
- **Mixed mode:** a station can run some pumps on `manual` and others automatic; shifts still exist for cash accountability.
