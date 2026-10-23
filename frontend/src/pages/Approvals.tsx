import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorNote } from "../components";
import { EndDetails, type HeldStage } from "./HeldEnd";

// cash_due: meters closed at the shift change, the attendant is still counting their cash (nothing to approve yet)
type End = Omit<HeldStage, "stage" | "end_id" | "can_fix" | "status"> & { id: string; shift_started_at: string; status: HeldStage["status"] | "cash_due" };
interface Undo { id: string; kind_label: string; reason: string; person: string; station: string; requested_by: string; created_at: string; end: End }
interface LogRow { at: string; action: string; what: string; by: string; station: string }

const time = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dayName = (d: string) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const localDate = (iso: string) => { const d = new Date(iso); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };

/** The log grouped by date like the dip history: newest day first, each day's actions newest first. */
const byDay = (rows: LogRow[]) => {
  const days = new Map<string, LogRow[]>();
  for (const r of [...rows].sort((a, b) => b.at.localeCompare(a.at))) days.set(localDate(r.at), [...(days.get(localDate(r.at)) ?? []), r]);
  return [...days.entries()];
};

/** Managers (their stations) and the owner approve attendants' ends of shift or send them back to fix, answer undo
 * requests, and see a log of what has been done. */
export default function Approvals() {
  const { user } = useAuth();
  const [ends, setEnds] = useState<End[] | null>(null);
  const [undos, setUndos] = useState<Undo[]>([]);
  const [log, setLog] = useState<LogRow[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [sending, setSending] = useState<string | null>(null); // the end being sent back
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const load = async () => {
    const [e, u, l] = await Promise.all([api<End[]>("/api/shift-ends/"), api<Undo[]>("/api/undo-requests/"), api<LogRow[]>("/api/approvals/log/").catch(() => [])]);
    setEnds(e); setUndos(u); setLog(l);
  };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  if (user?.role !== "owner" && user?.role !== "manager") return <Navigate to="/" replace />;

  const act = async (path: string, body: unknown, message: string) => {
    try { await api(path, { method: "POST", body }); setOk(message); setError(null); setSending(null); await load(); } catch (e) { setError(errorMessage(e)); }
  };
  const waiting = (ends ?? []).filter((e) => e.status === "pending");
  const sentBack = (ends ?? []).filter((e) => e.status === "sent_back");
  const cashDue = (ends ?? []).filter((e) => e.status === "cash_due");
  return (
    <>
      <h1>Approvals</h1>
      <ErrorNote message={error} />{ok && <p role="status" className="ok-note">{ok}</p>}
      {undos.length > 0 && (<>
        <h2>Undo requests</h2>
        {undos.map((u) => (
          <section key={u.id} className="card"><div className="row between"><strong>{u.person} · {u.station}</strong><Badge kind="past_due">{u.kind_label}</Badge></div>
            <p>Reason: {u.reason}</p>
            <p className="muted">Asked {time(u.created_at)}{u.requested_by !== u.person ? ` by ${u.requested_by}` : ""} · shift from {time(u.end.shift_started_at)}</p>
            <EndDetails end={u.end} />
            <label>Note (optional)<input value={notes[u.id] ?? ""} onChange={(e) => setNotes({ ...notes, [u.id]: e.target.value })} /></label>
            <div className="row gap">
              <button className="btn primary" onClick={() => void act(`/api/undo-requests/${u.id}/allow/`, { note: notes[u.id] ?? "" }, `Allowed: ${u.person} can now ${u.kind_label.toLowerCase()}.`)}>Allow</button>
              <button className="btn" onClick={() => void act(`/api/undo-requests/${u.id}/refuse/`, { note: notes[u.id] ?? "" }, `Refused. ${u.person} sees your answer.`)}>Refuse</button>
            </div>
          </section>
        ))}</>)}
      <h2>Shift ends to approve</h2>
      {!ends && !error && <p className="muted">Loading…</p>}
      {ends && waiting.length === 0 && <p className="muted">Nothing waiting. Attendants can start their next shift once their end is approved.</p>}
      {waiting.map((e) => (
        <section key={e.id} className="card"><div className="row between"><strong>{e.person} · {e.station}</strong><span className="muted">ended {time(e.ended_at)}</span></div>
          <EndDetails end={e} />
          {e.undo?.status === "pending" && <p className="warn">Asked to {e.undo.kind_label.toLowerCase()}: see the request above.</p>}
          {sending === e.id ? (
            <div className="form">
              <label>What needs fixing?<textarea value={notes[e.id] ?? ""} onChange={(ev) => setNotes({ ...notes, [e.id]: ev.target.value })} /></label>
              <div className="row gap"><button className="btn primary" onClick={() => void act(`/api/shift-ends/${e.id}/send-back/`, { note: notes[e.id] ?? "" }, `Sent back to ${e.person}.`)}>Send back</button>
                <button className="link" onClick={() => setSending(null)}>Cancel</button></div>
            </div>
          ) : (
            <div className="row gap">
              <button className="btn primary" onClick={() => void act(`/api/shift-ends/${e.id}/approve/`, {}, `Approved: ${e.person} can start their next shift.`)}>Approve</button>
              <button className="btn" onClick={() => setSending(e.id)}>Send back to fix</button>
            </div>
          )}
        </section>
      ))}
      {cashDue.length > 0 && (<>
        <h2>Meters closed, cash not handed in yet</h2>
        {cashDue.map((e) => <p key={e.id} className="card">{e.person} · {e.station}: closed their meters at {time(e.ended_at)} and is counting their cash. Expected {e.expected ?? "?"}.</p>)}</>)}
      {sentBack.length > 0 && (<>
        <h2>Sent back, waiting for the attendant</h2>
        {sentBack.map((e) => <p key={e.id} className="card">{e.person} · {e.station}: {e.note}</p>)}</>)}
      <h2>What has been done</h2>
      {log.length === 0 ? <p className="muted">Nothing in the last 14 days.</p> : byDay(log).map(([day, rows]) => (
        <section key={day} className="card" aria-label={`Approvals on ${dayName(day)}`}>
          <div className="row between"><h3>{dayName(day)}</h3><span className="muted">{rows.length} action{rows.length === 1 ? "" : "s"}</span></div>
          <ul className="plain hist" aria-label="Approvals log">
            {rows.map((r, i) => <li key={`${r.at}-${i}`}><span className="hist-time">{clock(r.at)}</span> <span><span className="muted">{r.station} · {r.by}:</span> {r.what}</span></li>)}
          </ul>
        </section>
      ))}
    </>
  );
}
