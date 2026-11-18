import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorNote } from "../components";

interface Reason { code: string; label: string; write_off: boolean }
/** A missing delivery's form, filled from the dips by the server. */
/** One tank's part of a delivery into joined tanks: how much its own dip rose, and the litres put into it. */
interface SplitPart { tank: string; name: string; rise: string; litres: string }
interface DeliveryForm { tank: string; tanks: { id: string; name: string }[]; fuel: string; litres: string; from: string; to: string; delivered_at: string; currency: string;
  split?: SplitPart[] }
interface Summary { open: number; awaiting_approval: number; to_review: number; reasons: Record<string, Reason[]> }
type Tab = "open" | "review" | "history";

const KIND: Record<string, string> = { loss: "Fuel loss", cash: "Cash difference", low_stock: "Low stock", missed_reading: "Missed reading", meter: "Meter numbers", delivery: "Delivery check" };
const STATUS: Record<string, [string, string]> = {
  open: ["Open", "suspended"], awaiting_approval: ["Waiting for owner approval", "past_due"],
  resolved: ["Resolved", "active"], auto_resolved: ["Closed by itself", "active"],
};
const EVENT: Record<string, string> = {
  opened: "Opened", reopened: "Came back and reopened", reminded: "Manager reminded", escalated: "Owner told",
  resolved: "Resolved", submitted_for_approval: "Sent to the owner for approval", approved: "Approved by the owner",
  reviewed: "Accepted by the owner", sent_back: "Sent back by the owner", auto_resolved: "Closed by itself", pattern: "Repeat spotted",
};
const when = (iso: string) => new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const age = (iso: string) => { const h = (Date.now() - new Date(iso).getTime()) / 36e5; return h < 1 ? "just now" : h < 48 ? `${Math.floor(h)} h` : `${Math.floor(h / 24)} days`; };

/** Alerts: what needs attention, what managers resolved (for the owner to review), and the full history. */
export default function Alerts() {
  const { user } = useAuth();
  const owner = user?.role === "owner";
  const [tab, setTab] = useState<Tab>("open");
  const [rows, setRows] = useState<any[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [filter, setFilter] = useState({ status: "", kind: "" });
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const q = new URLSearchParams({ view: tab, ...(tab === "history" && filter.status ? { status: filter.status } : {}), ...(tab === "history" && filter.kind ? { kind: filter.kind } : {}) });
      const [list, s] = await Promise.all([api<any>(`/api/alerts/?${q}`), api<Summary>("/api/alerts/summary/")]);
      setRows(list.results); setSummary(s);
    } catch (e) { setError(errorMessage(e)); }
  }, [tab, filter]);
  useEffect(() => { void load(); }, [load]);

  const act = async (path: string, body: any, done: string) => {
    setError(null);
    try { await api(path, { method: "POST", body }); setNote(done); await load(); } catch (e) { setError(errorMessage(e)); }
  };
  const refresh = async () => { await api("/api/alerts/refresh/", { method: "POST" }); setNote("Checked."); void load(); };

  const tabs: [Tab, string][] = [["open", `Needs attention${summary ? ` (${summary.open + summary.awaiting_approval})` : ""}`],
    ...(owner ? [["review", `To review${summary?.to_review ? ` (${summary.to_review})` : ""}`] as [Tab, string]] : []), ["history", "History"]];
  return (
    <>
      <div className="row between"><h1>Alerts</h1><button className="btn" onClick={() => void refresh()}>Check now</button></div>
      <div className="chips" role="tablist">{tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={`chip ${tab === k ? "on" : ""}`} onClick={() => { setTab(k); setNote(null); }}>{l}</button>)}</div>
      {tab === "history" && (
        <div className="row gap">
          <label>Status<select value={filter.status} onChange={(e) => setFilter({ ...filter, status: e.target.value })}>
            <option value="">All</option>{Object.entries(STATUS).map(([k, [l]]) => <option key={k} value={k}>{l}</option>)}</select></label>
          <label>Kind<select value={filter.kind} onChange={(e) => setFilter({ ...filter, kind: e.target.value })}>
            <option value="">All</option>{Object.entries(KIND).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
        </div>
      )}
      {note && <p role="status" className="ok-note">{note}</p>}
      <ErrorNote message={error} />
      {rows.length === 0 && <p className="muted center">{tab === "open" ? "All good. Nothing needs attention." : tab === "review" ? "Nothing to review." : "No alerts yet."}</p>}
      {rows.map((a) => <AlertCard key={a.id} a={a} tab={tab} owner={owner} reasons={summary?.reasons[a.kind] ?? []} act={act} />)}
    </>
  );
}

function AlertCard({ a, tab, owner, reasons, act }: { a: any; tab: Tab; owner: boolean; reasons: Reason[]; act: (p: string, b: any, done: string) => Promise<void> }) {
  const [resolving, setResolving] = useState(false);
  const [reason, setReason] = useState("");
  const [text, setText] = useState("");
  const [story, setStory] = useState<any[] | null>(null);
  const picked = reasons.find((r) => r.code === reason);
  const needsNote = picked && (picked.code === "other" || picked.write_off);
  const [label, kind] = STATUS[a.status] ?? [a.status, "active"];
  const showStory = async () => setStory(story ? null : (await api<any>(`/api/alerts/${a.id}/`)).events);

  return (
    <section className={`card alert ${a.gain ? "good" : a.status === "open" && a.severity === "critical" ? "bad" : ""}`}>
      <div className="row between gap">
        <span><Badge kind={a.gain ? "active" : a.severity === "critical" ? "suspended" : a.severity === "warning" ? "past_due" : "active"}>{a.gain ? "Fuel gain" : KIND[a.kind] ?? a.kind}</Badge> <span className="muted">{a.station_name}</span></span>
        <Badge kind={kind}>{label}</Badge>
      </div>
      <p>{a.message}</p>
      <p className="muted">{a.status === "open" ? `Open ${age(a.created_at)}` : `Raised ${when(a.created_at)}`}{a.reopened_count ? ` · came back ${a.reopened_count}×` : ""}{a.escalated_at ? " · owner told" : a.reminded_at ? " · manager reminded" : ""}</p>
      {a.pattern && <p className="warn">{a.pattern}</p>}
      {a.status === "auto_resolved" && a.resolution_note && <p className="resolution"><strong>Cleared by itself:</strong> {a.resolution_note}</p>}
      {a.resolution_label && (
        <p className="resolution"><strong>{a.status === "awaiting_approval" ? "Proposed" : "How"}:</strong> {a.resolution_label}{a.resolution_note ? `. “${a.resolution_note}”` : ""}
          {a.resolved_by_name && <span className="muted"> · {a.resolved_by_name} ({a.resolved_by_role})</span>}
          {a.reviewed_by_name && <span className="muted"> · accepted by {a.reviewed_by_name}</span>}</p>
      )}

      {a.delivery_form && !resolving && <RecordDelivery alertId={a.id} form={a.delivery_form} act={act} />}
      {a.status === "open" && !resolving && <button className={a.delivery_form ? "link" : "btn primary"} onClick={() => setResolving(true)}>
        {a.delivery_form ? "Not a delivery? Resolve another way" : "Resolve"}</button>}
      {a.status === "open" && resolving && (
        <div className="form">
          <label>How was it resolved?<select value={reason} onChange={(e) => setReason(e.target.value)}>
            <option value="">Choose…</option>{reasons.map((r) => <option key={r.code} value={r.code}>{r.label}</option>)}</select></label>
          {picked?.write_off && !owner && <p className="muted">This writes off fuel or money, so it goes to the owner for approval.</p>}
          <label>Note{needsNote ? " (required)" : " (optional)"}<textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="What was found and what was done" /></label>
          <div className="row gap">
            <button className="btn primary" disabled={!reason || (needsNote && !text.trim())}
              onClick={() => void act(`/api/alerts/${a.id}/resolve/`, { reason, note: text }, picked?.write_off && !owner ? "Sent to the owner for approval." : "Resolved.")}>
              {picked?.write_off && !owner ? "Send for approval" : "Resolve"}</button>
            <button className="btn" onClick={() => setResolving(false)}>Cancel</button>
          </div>
        </div>
      )}

      {owner && tab === "review" && (
        <div className="form">
          <label>Note (required to send back)<textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="What still needs doing" /></label>
          <div className="row gap">
            {a.status === "awaiting_approval"
              ? <button className="btn primary" onClick={() => void act(`/api/alerts/${a.id}/approve/`, { note: text }, "Write-off approved.")}>Approve</button>
              : <button className="btn primary" onClick={() => void act(`/api/alerts/${a.id}/accept/`, {}, "Accepted.")}>Accept</button>}
            <button className="btn" disabled={!text.trim()} onClick={() => void act(`/api/alerts/${a.id}/send-back/`, { note: text }, "Sent back to the manager.")}>Send back</button>
          </div>
        </div>
      )}

      <button className="link" onClick={() => void showStory()}>{story ? "Hide what happened" : "What happened"}</button>
      {story && (
        <ol className="story">
          {story.map((e) => <li key={e.id}><span className="muted">{when(e.created_at)}</span> {EVENT[e.kind] ?? e.kind}{e.data?.label ? `: ${e.data.label}` : ""}{e.actor_name ? ` · ${e.actor_name}` : ""}{e.note && e.kind !== "opened" ? ` · “${e.note}”` : ""}</li>)}
        </ol>
      )}
    </section>
  );
}

/** "YYYY-MM-DDTHH:mm" in this phone's time, for a datetime-local box. */
const localInput = (iso: string) => {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
};

/** The delivery the dips say came in, ready to save: litres, fuel and tank from the dips, a time between them.
 *  Supplier, value and the delivery note number can be left blank and added later on the Dip screen. */
function RecordDelivery({ alertId, form, act }: { alertId: string; form: DeliveryForm; act: (p: string, b: any, done: string) => Promise<void> }) {
  const [d, setD] = useState({ tank: form.tank, litres: String(Number(form.litres)), at: localInput(form.delivered_at), supplier: "", value: "", note: "" });
  // Joined tanks: the litres per tank, from each tank's own dips (form.split); empty when the dips cannot tell.
  const splitting = (form.split?.length ?? 0) > 0;
  const [parts, setParts] = useState<Record<string, string>>(() => Object.fromEntries((form.split ?? []).map((p) => [p.tank, String(Number(p.litres))])));
  const splitTotal = Object.values(parts).reduce((s, v) => s + (Number(v) || 0), 0);
  const pending = [!d.supplier.trim() && "supplier", !d.value && "value", !d.note.trim() && "delivery note number"].filter(Boolean) as string[];
  const into = splitting ? { split: Object.entries(parts).map(([tank, litres]) => ({ tank, litres: litres || "0" })) } : { tank: d.tank, litres: d.litres };
  const save = () => void act(`/api/alerts/${alertId}/record-delivery/`, {
    ...into, delivered_at: new Date(d.at).toISOString(), supplier: d.supplier.trim(), value: d.value || null, note_number: d.note.trim(),
  }, pending.length ? `Delivery recorded. Add the ${pending.join(", ")} on the Dip screen when you have them.` : "Delivery recorded.");
  const ready = splitting ? splitTotal > 0 && Object.values(parts).every((v) => v === "" || Number(v) >= 0) : !!d.litres && Number(d.litres) > 0;
  return (
    <form className="form" aria-label="Record the delivery" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h3>Record the delivery</h3>
      <p className="muted">Filled in from the dips. Check the litres against the delivery note if you have it.</p>
      <p>Fuel: <strong>{form.fuel}</strong>{form.tanks.length === 1 ? <> · into {form.tanks[0].name}</> : null}</p>
      {splitting ? <>
        <p className="muted">Split by how much each tank's dip rose. The litres sold in between are shared the same way.</p>
        {form.split!.map((p) => (
          <label key={p.tank}>{`${p.name} (dip rose ${Number(p.rise).toLocaleString()} L)`}
            <input inputMode="decimal" value={parts[p.tank]} onChange={(e) => setParts({ ...parts, [p.tank]: e.target.value.replace(",", ".") })} /></label>
        ))}
        <p>Total: <strong>{`${splitTotal.toLocaleString()} L`}</strong></p>
      </> : <>
        {form.tanks.length > 1 && (
          <label>Into tank<select value={d.tank} onChange={(e) => setD({ ...d, tank: e.target.value })}>
            {form.tanks.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label>
        )}
        <label>Litres received<input inputMode="decimal" value={d.litres} onChange={(e) => setD({ ...d, litres: e.target.value.replace(",", ".") })} required /></label>
      </>}
      <label>Delivered at<input type="datetime-local" value={d.at} min={localInput(form.from)} max={localInput(form.to)} onChange={(e) => setD({ ...d, at: e.target.value })} required /></label>
      <p className="muted">Between the two dips, so the fuel check balances.</p>
      <label>Supplier<input value={d.supplier} placeholder="Pending" onChange={(e) => setD({ ...d, supplier: e.target.value })} /></label>
      <label>{`Value (${form.currency})`}<input inputMode="decimal" value={d.value} placeholder="Pending" onChange={(e) => setD({ ...d, value: e.target.value.replace(",", ".") })} /></label>
      <label>Delivery note no.<input value={d.note} placeholder="Pending" onChange={(e) => setD({ ...d, note: e.target.value })} /></label>
      {pending.length > 0 && <p className="muted">{`Pending: ${pending.join(", ")}. You can save now and add them later.`}</p>}
      <button className="btn primary" disabled={!ready || !d.at}>Save delivery</button>
    </form>
  );
}
