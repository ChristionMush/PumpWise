import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";

export default function Settings() {
  const [s, setS] = useState<any>(null);
  const [exports, setExports] = useState<any[]>([]);
  const [users, setUsers] = useState<any[]>([]);
  const [nu, setNu] = useState({ full_name: "", phone: "", role: "attendant", password: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = async () => { const [a, b, c] = await Promise.all([api("/api/settings/"), api("/api/exports/"), api("/api/users/")]); setS(a); setExports(b); setUsers(c.results); };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  const wrap = (fn: () => Promise<any>, ok: string) => async () => { try { await fn(); setMsg(ok); setError(null); await load(); } catch (e) { setError(errorMessage(e)); } };
  if (!s) return <ErrorNote message={error} />;
  const field = (k: string, label: string) => <label>{label}<input value={s[k] ?? ""} onChange={(e) => setS({ ...s, [k]: e.target.value })} /></label>;
  const download = async (id: string) => { const r = await api<{ url: string }>(`/api/exports/${id}/link/`, { method: "POST" }); window.location.href = r.url; };
  return (
    <>
      <h1>Settings</h1><ErrorNote message={error} />{msg && <p role="status" className="ok-note">{msg}</p>}
      <section className="card form"><h2>Daily message to the owner</h2>
        {field("summary_phone", "Phone number")}
        <label>Send by<select value={s.summary_channel} onChange={(e) => setS({ ...s, summary_channel: e.target.value })}><option value="sms">SMS</option><option value="whatsapp">WhatsApp</option><option value="none">Off</option></select></label>
        {field("display_name", "Business name shown in the app")}
        <h2>When to warn me</h2>
        {field("loss_threshold_litres", "Fuel loss above (litres)")}{field("loss_threshold_pct", "or above (% of fuel sold)")}
        {field("cash_variance_amount", "Cash difference above (amount)")}{field("cash_variance_pct", "or above (% of sales)")}
        {field("low_stock_days", "Low stock when days left is")}{field("refill_lead_days", "Order this many days before empty")}
        <button className="btn primary" onClick={wrap(() => api("/api/settings/", { method: "PATCH", body: { ...s, logo_url: undefined } }), "Saved.")}>Save settings</button></section>
      <section className="card form"><h2>Team</h2>
        {users.map((u) => <p key={u.id}>{u.full_name} <span className="muted">· {u.role} · {u.phone ?? u.email}</span></p>)}
        <label>Name<input value={nu.full_name} onChange={(e) => setNu({ ...nu, full_name: e.target.value })} /></label>
        <label>Phone<input inputMode="tel" value={nu.phone} onChange={(e) => setNu({ ...nu, phone: e.target.value })} /></label>
        <label>Role<select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}><option value="attendant">Attendant</option><option value="manager">Manager</option></select></label>
        <label>Password<input type="password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} /></label>
        <button className="btn" onClick={wrap(async () => { await api("/api/users/", { method: "POST", body: { ...nu, phone: nu.phone || null } }); setNu({ full_name: "", phone: "", role: "attendant", password: "" }); }, "User added.")}>Add user</button></section>
      <section className="card form"><h2>Your data</h2><p className="muted">Download everything we hold for your business (CSV and JSON). The link works for 24 hours.</p>
        <button className="btn" onClick={wrap(() => api("/api/exports/", { method: "POST" }), "Preparing your export. Refresh in a minute.")}>Export my data</button>
        {exports.map((e) => <div key={e.id} className="row between"><span>{new Date(e.created_at).toLocaleString()} · {e.status}</span>{e.status === "ready" && <button className="btn" onClick={() => void download(e.id)}>Download</button>}</div>)}</section>
    </>
  );
}
