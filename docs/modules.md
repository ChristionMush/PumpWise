# PumpWise modules and Marks (draft for review)

Status: draft, nothing built. Every module below is read from what the code does today.

## Marks

| Mark | Based on | Who it suits |
|---|---|---|
| **Mark 1** | Fuel Zone | A site manager on the forecourt. The manager allocates pumps, dips the tanks and closes the day from 21:00. Attendants only sell and hand in cash. |
| **Mark 2** | PESNAT | No site manager on the forecourt. Attendants pick their pumps, dip at each shift exchange and pay small expenses. The day runs handover to handover. The owner/manager reads the results remotely. |

A Mark is a starting set of ticks. The code only checks modules, never Mark names.

Marks live in the database (`tenants_mark`, platform only; migration 0019 adds Mark 1 and Mark 2 and links each
business to its closest Mark). Each business keeps its own modules, so editing a Mark never changes a business on it.
"Duplicate" copies a Mark to change into a new one, and the original stays as it was. The platform API:
`/api/platform/modules/`, `/api/platform/marks/` (list; POST to create, or `duplicate_of`), `/api/platform/marks/<id>/`
(PATCH name, description, modules), `/api/platform/tenants/<id>/mark/` (link a business). A business's summary shows
its `mark`, `modules` and `mark_differences`.

## Core (every client, cannot be unticked)

Shifts with meter numbers and cash hand-in · shift-end approval and undo requests · dips and deliveries (managers/owners) · tank fuel check · prices and exchange rates · stations, tanks, pumps, nozzles and dip charts · "also draws from" tanks (set per nozzle in setup) · daily report and reports · dashboard · alerts · audit log · exports · daily summary message · offline recording.

## Modules

| # | Module | Kind | Choices | Mark 1 | Mark 2 | Needs | Stored today in |
|---|---|---|---|---|---|---|---|
| 1 | **Pump allocation** | pick one | Shared, one attendant · Shared, several attendants · Assigned by manager · Attendants pick | Assigned by manager | Attendants pick | "Assigned…" and "Attendants pick" need several attendants | `pump_system`, `multi_attendant_enabled`, `pump_assignment_enabled`, `attendants_pick_pumps` (+ each station's modes) |
| 2 | **Expenses** | on/off | | off | on | | `expenses_enabled` |
| 3 | **Attendants dip at shift end** | on/off | | off | on | | `dip_at_shift_end` |
| 4 | **Attendants see dip litres** | on/off | | off | on | module 3 | `attendants_see_dip_litres` |
| 5 | **Day close** | pick one | Manager closes from 21:00 · At the 07:00 change · At the 18:00 change | Manager from 21:00 | At the evening handover (18:00 setting) | | `close_day_at_change` |

Notes:
- Module 1 is the only one that has to be settled before the first shift. Changing it is refused while a shift is open, and every other module will get the same guard.
- Pump allocation is chosen for the business and puts every station on it (a new station follows it). Each station keeps
  its own setting (assigned pumps, several attendants), and the shift code reads the station's, which is correct. Only
  "attendants pick" is read from the business.
- Today, "Attendants pick" is a separate tick on top of "Assigned". As a module, it becomes a fourth choice so the screen can't show a contradictory mix. The data stays where it is.
- Module 5, "at a shift change": the handover time is human and varies. The 18:00 setting already counts a handover
  any time from 15:00 to 20:00 (3 hours before to 2 hours after), so PESNAT's 16:00, 18:00 and 20:00 handovers all close
  the day. The 07:00 setting works the same way (04:00 to 09:00).
- Module 4 deliberately breaks rule 11 (attendants never see tank litres). That's why it stays off unless chosen.

## Wanted but not built yet

None for now. (A limited manager view for Mark 2 was discussed; not wanted yet.)

## Not modules (settings every client already has)

Loss and cash-variance limits · low-stock days and refill lead days · summary channel and phone · logo and display name · station currency. These stay as they are, under Settings.

## Live settings (read from the server, 2026-11-13)

| Business | Pump allocation | Expenses | Dip at shift end | See dip litres | Day close | Stations | Matches |
|---|---|---|---|---|---|---|---|
| Fuel Zone | Assigned by manager | off | off | off | Manager from 21:00 | Eastview Mutamba, Usheokunze Chigwende (both several/assigned) | Mark 1 exactly |
| PESNAT | Attendants pick | on | on | on | 18:00 | Mverechena Domboshava (several/assigned) | Mark 2 exactly |
| Litrix (demo) | Attendants pick | on | on | **off** | 18:00 | Cowdray Park Station (several/assigned) | Mark 2 with one module unticked |

These are the settings the "today's behaviour" tests lock in.

## Decided (2026-11-13)

- Mark 2 day close: at the handover, whenever it happens (the 18:00 setting, which counts 15:00 to 20:00).
- Limited manager view: not for now.
- No other Fuel Zone vs PESNAT differences beyond the five modules.
- Pricing: not tied to Marks or modules.
