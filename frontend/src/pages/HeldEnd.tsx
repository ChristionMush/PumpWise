import { useState } from "react";
import { api, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";

export interface EndMeter { nozzle: string; pump: string; label: string; fuel_code: string; start: string | null; end: string | null; litres: string | null; amount: string | null }
export interface EndCash { payment_type: string; currency: string; amount: string }
export interface UndoInfo { id: string; kind: string; kind_label: string; status: "pending" | "allowed" | "refused" | "used"; reason: string; decision_note: string }
/** The attendant's last end of shift: waiting for the manager, sent back to fix, or a change a manager or the owner allowed. */
export interface HeldStage {
  stage: "held"; end_id: string; shift_id: string; station: string; status: "pending" | "sent_back" | "approved"; ended_at: string; note: string;
  person: string; currency: string; meters: EndMeter[]; cash: EndCash[]; handed_in: string; expected: string | null; cash_difference: string | null;
  undo: UndoInfo | null; can_fix: { cash: boolean; meters: boolean };
}

const TYPES = [["cash", "Cash"], ["mobile_money", "EcoCash / mobile money"], ["card", "Card / swipe"], ["credit", "Credit / coupon"]] as const;
const LABEL: Record<string, string> = Object.fromEntries(TYPES);
const time = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });

/** Meters and cash of one end of shift, with the cash difference when it is this person's own. */
export function EndDetails({ end }: { end: Pick<HeldStage, "meters" | "cash" | "currency" | "handed_in" | "expected" | "cash_difference"> }) {
  return (
    <>
      {end.meters.map((m) => (
        <p key={m.nozzle} className="muted">{m.pump} · {m.label}: {m.start ?? "?"} → {m.end ?? "not ended"}{m.litres ? ` · ${Number(m.litres).toLocaleString()} L` : ""}</p>
      ))}
      <p>{end.cash.length ? end.cash.map((c) => `${LABEL[c.payment_type] ?? c.payment_type} ${c.currency} ${Number(c.amount).toFixed(2)}`).join(" · ") : "No cash handed in"}</p>
      {end.expected != null && (
        <p>Expected {end.currency} {Number(end.expected).toFixed(2)} · handed in {Number(end.handed_in).toFixed(2)} · cash difference <strong>{Number(end.cash_difference).toFixed(2)}</strong></p>
      )}
    </>
  );
}

export default function HeldEnd({ stage, onChanged }: { stage: HeldStage; onChanged: () => void }) {
  const fixing = stage.can_fix.cash || stage.can_fix.meters;
  const own = stage.cash.filter((c) => c.currency === stage.currency);
  const [cash, setCash] = useState<Record<string, string>>(() => Object.fromEntries(own.map((c) => [c.payment_type, c.amount])));
  const [meters, setMeters] = useState<Record<string, string>>(() => Object.fromEntries(stage.meters.filter((m) => m.end).map((m) => [m.nozzle, m.end!])));
  const [asking, setAsking] = useState(false);
  const [kind, setKind] = useState("cash");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const call = async (path: string, body: unknown, message: string) => {
    setBusy(true); setError(null);
    try { await api(path, { method: "POST", body }); setDone(message); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };
  const fix = () => call(`/api/shift-ends/${stage.end_id}/fix/`, {
    cash: stage.can_fix.cash ? [
      ...TYPES.filter(([t]) => cash[t] || own.some((c) => c.payment_type === t)).map(([t]) => ({ payment_type: t, currency: stage.currency, amount: cash[t] || "0" })),
      ...stage.cash.filter((c) => c.currency !== stage.currency), // other currencies stay as they were
    ] : [],
    readings: stage.can_fix.meters ? Object.entries(meters).map(([nozzle, reading]) => ({ nozzle, reading })) : [],
  }, "Fixed. It is back with your manager for approval.");
  const ask = () => {
    if (!reason.trim()) return setError("Say why, so the manager or the owner can decide.");
    return call(`/api/shift-ends/${stage.end_id}/undo/`, { kind, reason }, "Sent to the manager and the owner. You will see the answer here.");
  };

  if (done) return (
    <section className="card" role="status"><h1>Sent</h1><p>{done}</p><button className="btn primary" onClick={onChanged}>OK</button></section>
  );
  const heading = stage.status === "sent_back" ? "Your manager sent your shift back" : stage.status === "pending" ? "Waiting for your manager" : "You may change your shift";
  return (
    <>
      <div className="row between"><h1>{heading}</h1><Badge kind={stage.status === "sent_back" ? "suspended" : "past_due"}>{stage.status === "sent_back" ? "fix it" : stage.status === "pending" ? "waiting" : "allowed"}</Badge></div>
      <section className="card">
        <p>Your shift at {stage.station}, ended {time(stage.ended_at)}.</p>
        {stage.status === "pending" && <p>Your manager checks it now. You can start a new shift once it is approved.</p>}
        {stage.status === "sent_back" && <p className="warn">Manager's note: {stage.note}</p>}
        <EndDetails end={stage} />
      </section>
      {fixing && (
        <section className="card form"><h2>Fix it</h2>
          {stage.can_fix.meters && stage.meters.filter((m) => m.end).map((m) => (
            <label key={m.nozzle}>{m.pump} · {m.label}: end meter<input inputMode="decimal" value={meters[m.nozzle] ?? ""} onChange={(e) => setMeters({ ...meters, [m.nozzle]: e.target.value.replace(",", ".") })} /></label>
          ))}
          {stage.can_fix.cash && TYPES.map(([t, label]) => (
            <label key={t}>{label} ({stage.currency})<input inputMode="decimal" value={cash[t] ?? ""} onChange={(e) => setCash({ ...cash, [t]: e.target.value })} /></label>
          ))}
          <button className="btn primary big" disabled={busy} onClick={() => void fix()}>Send the fix to my manager</button>
        </section>
      )}
      {stage.undo && stage.undo.status === "pending" && <p className="card" role="status">Waiting for an answer: {stage.undo.kind_label.toLowerCase()}.</p>}
      {stage.undo && stage.undo.status === "refused" && <p className="card warn" role="status">The answer was no to "{stage.undo.kind_label.toLowerCase()}"{stage.undo.decision_note ? `: ${stage.undo.decision_note}` : "."}</p>}
      {stage.status !== "sent_back" && !fixing && stage.undo?.status !== "pending" && (asking ? (
        <section className="card form"><h2>Ask to undo</h2>
          <label>What<select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="cash">Change the cash I handed in</option>
            {stage.meters.some((m) => m.end) && <option value="meters">Change my end meter numbers</option>}
            <option value="reopen">I ended my shift by mistake</option>
          </select></label>
          <label>Why<textarea value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <div className="row gap"><button className="btn primary" disabled={busy} onClick={() => void ask()}>Send for approval</button><button className="link" onClick={() => setAsking(false)}>Cancel</button></div>
        </section>
      ) : <button className="btn" onClick={() => setAsking(true)}>Ask to undo</button>)}
      <ErrorNote message={error} />
    </>
  );
}
