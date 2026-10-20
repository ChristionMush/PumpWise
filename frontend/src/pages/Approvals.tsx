import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorNote } from "../components";
import { EndDetails, type HeldStage } from "./HeldEnd";

type End = Omit<HeldStage, "stage" | "end_id" | "can_fix"> & { id: string; shift_started_at: string };
interface Undo { id: string; kind_label: string; reason: string; person: string; station: string; requested_by: string; created_at: string; end: End }

const time = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Managers approve attendants' ends of shift (or send them back to fix); the owner also answers undo requests. */
export default function Approvals() {
  const { user } = useAuth();
  const owner = user?.role === "owner";
  const [ends, setEnds] = useState<End[] | null>(null);
  const [undos, setUndos] = useState<Undo[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [sending, setSending] = useState<string | null>(null); // the end being sent back
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const load = async () => {
    const [e, u] = await Promise.all([api<End[]>("/api/shift-ends/"), owner ? api<Undo[]>("/api/undo-requests/") : Promise.resolve([])]);
    setEnds(e); setUndos(u);
  };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  if (user?.role !== "owner" && user?.role !== "manager") return <Navigate to="/" replace />;

  const act = async (path: string, body: unknown, message: string) => {
    try { await api(path, { method: "POST", body }); setOk(message); setError(null); setSending(null); await load(); } catch (e) { setError(errorMessage(e)); }
  };
  const waiting = (ends ?? []).filter((e) => e.status === "pending");
  const sentBack = (ends ?? []).filter((e) => e.status === "sent_back");
  return (
    <>
      <h1>Approvals</h1>
      <ErrorNote message={error} />{ok && <p role="status" className="ok-note">{ok}</p>}
      {owner && undos.length > 0 && (<>
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
          {e.undo?.status === "pending" && <p className="warn">Asked the owner: {e.undo.kind_label.toLowerCase()}.</p>}
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
      {sentBack.length > 0 && (<>
        <h2>Sent back, waiting for the attendant</h2>
        {sentBack.map((e) => <p key={e.id} className="card">{e.person} · {e.station}: {e.note}</p>)}</>)}
    </>
  );
}
