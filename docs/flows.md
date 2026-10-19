# PumpWise: use cases and flows

How the system behaves, drawn from the code (`backend/apps/operations/services.py`, `apps/insights/alert_actions.py`,
`apps/insights/alerts.py`, `frontend/src/pages/Shift.tsx`, `frontend/src/offline.ts`). Keep these in step with the code.

## 1. Use cases: who does what

![Use case diagram: who does what in PumpWise](use-cases.svg)

Attendants never see tank data (rule 11) and never assign or swap pumps; a manager acts only on their own station.

## 2. Starting a shift

```mermaid
flowchart LR
    S([Start shift]) --> M{Pumps at this station}
    M -->|assigned| A1{Pumps given to me?}
    A1 -->|none| A1x[Refused: manager assigns pumps]
    A1 -->|yes| A2[Open the shift, or join the open one]
    A2 --> A3{Pump still with someone?}
    A3 -->|free| A6[My pump period starts at my numbers]
    A3 -->|yes, online| A5x[Refused until they end their part]
    A3 -->|yes, offline phone| A7[Handover at my numbers, flagged]
    A7 --> A6

    M -->|shared, several attendants| B1{Shift open?}
    B1 -->|no| B2[Open it, I lead]
    B1 -->|yes| B3[Join: I hand in my cash]

    M -->|shared, one attendant| C1{Shift open?}
    C1 -->|no| C2[Open it]
    C1 -->|yes, mine| C3[My shift back]
    C1 -->|someone else on it| C4[Refused, kept for manager]
    C1 -->|previous person left| C5[Handover onto it]
```

Never two open shifts on the same pumps: their litres would be counted twice.

## 3. Assigned pumps: one shift, many attendants, handovers

```mermaid
sequenceDiagram
    actor A as Attendant A (morning)
    actor B as Attendant B (afternoon)
    actor M as Manager
    participant S as Server
    A->>S: Start on Pump 1 and Pump 2 at their meter numbers
    S-->>A: Shift opened, a pump period starts on each pump
    B->>S: Start on Pump 3
    S-->>B: B joins the same shift
    M->>S: Pump 2 to B, swap now, with both nozzles' numbers
    S-->>M: A's Pump 2 period ends, B's starts, at those numbers
    M->>S: Pump 1 to B next (no numbers: a planned handover)
    S-->>M: A keeps Pump 1 until A ends their part
    A->>S: End my part: Pump 1 numbers and A's cash
    S-->>A: A is done. Pump 1 passes straight to B at those numbers
    Note over B,S: B's phone shows Pump 1 within a minute
    B->>S: End my part, the last person on the shift
    S-->>B: End readings, shift closes, each person's litres and cash checked
```

Every litre belongs to exactly one person's pump period, so each person's expected cash is their own. If the
incoming attendant is not on the shift yet, they take the pump when they start, at the numbers it was left at.

## 4. What the attendant's phone shows

```mermaid
stateDiagram-v2
    [*] --> StartShift
    StartShift --> OnShift: Start shift (saved on the phone first)
    OnShift --> Waiting: End shift with no signal
    Waiting --> StartShift: sent, shift closed
    Waiting --> PartDone: sent, others still on the shift
    OnShift --> StartShift: End shift, shift closes
    OnShift --> PartDone: End my part (assigned pumps)
    PartDone --> StartShift: the shift closes
    StartShift --> Busy: one-attendant station, someone else on shift
    Busy --> StartShift: that shift ends or is handed over
```

The server says which stage each person is at (`my_stage` in prefill), so signing out and in, or another phone, lands on the
same screen. Only entries this phone has not sent yet can be newer than what the server knows.

## 5. Offline entries

```mermaid
flowchart TD
    E[Entry on the phone] --> Q[(Queue on the phone)]
    Q -->|signal returns, every 30 s| B[One batch per shift]
    B --> D{op_id seen before?}
    D -->|yes| R1[Return the stored result, change nothing]
    D -->|no| V{Shift reviewed, unknown, or not theirs?}
    V -->|yes| R2[Rejected, kept for a manager]
    V -->|no| AP[Apply: odd numbers are flagged, never refused]
    R1 & R2 & AP --> X[Remove from the queue]
```

## 6. Alert lifecycle

```mermaid
stateDiagram-v2
    [*] --> Open: rule fires (hourly check)
    Open --> Resolved: manager or owner, with reason and note
    Open --> AwaitingApproval: manager picks a write-off
    AwaitingApproval --> Resolved: owner approves
    AwaitingApproval --> Open: owner sends back, with a note
    Resolved --> Accepted: owner accepts (owner's own resolutions are accepted)
    Resolved --> Open: owner sends back, with a note
    Open --> ClosedByItself: low stock or missed dip clears
    ClosedByItself --> Open: same problem within 24 h (reopened)
    Resolved --> Open: low stock or missed dip still there 24 h later
    Open --> Open: 12 h reminder to manager, 24 h owner told
```

A loss or cash difference a person resolved stays resolved: it is still in that day's numbers, so the hourly check
never brings it back. Every step is an `AlertEvent`, shown as "What happened".
