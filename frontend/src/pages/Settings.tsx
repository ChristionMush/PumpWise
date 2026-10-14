import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";

export default function Settings() {
  const [s, setS] = useState<any>(null);
  const [exports, setExports] = useState<any[]>([]);
  const [users, setUsers] = useState<any[]>([]);
  const [stations, setStations] = useState<any[]>([]);
  const [nu, setNu] = useState({ full_name: "", phone: "", role: "attendant", password: "", station: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = async () => {
    const [a, b, c, d] = await Promise.all([api("/api/settings/"), api("/api/exports/"), api("/api/users/"), api("/api/stations/")]);
    setS(a); setExports(b); setUsers(c.results); setStations(d.results);
  };
  // One manager per station: a manager runs one station. Attendants work at one station or all of them.
  const setStation = (u: any, station: string) => api(`/api/users/${u.id}/stations/`, { method: "PUT", body: { station_ids: station ? [station] : [] } });
  const managerOf = (stationId: string) => users.find((u) => u.role === "manager" && u.station_ids?.includes(stationId));
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
        {users.map((u) => (
          <div key={u.id} className="row between gap"><span>{u.full_name} <span className="muted">· {u.role} · {u.phone ?? u.email}</span></span>
            {(u.role === "manager" || u.role === "attendant") && (
              <select aria-label={`Station of ${u.full_name}`} value={u.station_ids?.length === 1 ? u.station_ids[0] : ""}
                onChange={(e) => void wrap(() => setStation(u, e.target.value), `${u.full_name}: station saved.`)()}>
                <option value="">{u.role === "manager" ? "No station yet" : u.station_ids?.length > 1 ? "Several stations" : "All stations"}</option>
                {stations.map((st) => {
                  const other = u.role === "manager" ? managerOf(st.id) : undefined;
                  return <option key={st.id} value={st.id} disabled={Boolean(other && other.id !== u.id)}>{st.name}{other && other.id !== u.id ? ` (managed by ${other.full_name})` : ""}</option>;
                })}
              </select>
            )}
          </div>
        ))}
        <label>Name<input value={nu.full_name} onChange={(e) => setNu({ ...nu, full_name: e.target.value })} /></label>
        <label>Phone<input inputMode="tel" value={nu.phone} onChange={(e) => setNu({ ...nu, phone: e.target.value })} /></label>
        <label>Role<select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}><option value="attendant">Attendant</option><option value="manager">Manager</option></select></label>
        <label>Password<input type="password" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} /></label>
        <label>Station<select value={nu.station} onChange={(e) => setNu({ ...nu, station: e.target.value })}>
          <option value="">{nu.role === "manager" ? "Choose later" : "All stations"}</option>
          {stations.map((st) => <option key={st.id} value={st.id} disabled={nu.role === "manager" && Boolean(managerOf(st.id))}>{st.name}{nu.role === "manager" && managerOf(st.id) ? ` (managed by ${managerOf(st.id).full_name})` : ""}</option>)}
        </select></label>
        <button className="btn" onClick={wrap(async () => {
          const { station, ...person } = nu;
          const created = await api<any>("/api/users/", { method: "POST", body: { ...person, phone: person.phone || null } });
          if (station) await setStation(created, station);
          setNu({ full_name: "", phone: "", role: "attendant", password: "", station: "" });
        }, "User added.")}>Add user</button></section>
      <section className="card form"><h2>Your data</h2><p className="muted">Download everything we hold for your business (CSV and JSON). The link works for 24 hours.</p>
        <button className="btn" onClick={wrap(() => api("/api/exports/", { method: "POST" }), "Preparing your export. Refresh in a minute.")}>Export my data</button>
        {exports.map((e) => <div key={e.id} className="row between"><span>{new Date(e.created_at).toLocaleString()} · {e.status}</span>{e.status === "ready" && <button className="btn" onClick={() => void download(e.id)}>Download</button>}</div>)}</section>
    </>
  );
}
