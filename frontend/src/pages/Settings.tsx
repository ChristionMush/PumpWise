import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, PasswordInput } from "../components";

export default function Settings() {
  const { user } = useAuth();
  const owner = user?.role === "owner";
  const [editing, setEditing] = useState<string | null>(null); // the team member being edited
  const [s, setS] = useState<any>(null);
  const [exports, setExports] = useState<any[]>([]);
  const [users, setUsers] = useState<any[]>([]);
  const [stations, setStations] = useState<any[]>([]);
  const [nu, setNu] = useState({ full_name: "", phone: "", role: "attendant", password: "", station: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // managers see the team (and their own account) only; the business settings and data exports are the owner's
  const load = async () => {
    const [c, d] = await Promise.all([api<any>("/api/users/"), api<any>("/api/stations/")]);
    setUsers(c.results); setStations(d.results);
    if (owner) {
      const [a, b] = await Promise.all([api("/api/settings/"), api<any[]>("/api/exports/")]);
      setS(a); setExports(b);
    } else setS({});
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
      <h1>{owner ? "Settings" : "Team"}</h1><ErrorNote message={error} />{msg && <p role="status" className="ok-note">{msg}</p>}
      {owner && <section className="card form"><h2>Daily message to the owner</h2>
        {field("summary_phone", "Phone number")}
        <label>Send by<select value={s.summary_channel} onChange={(e) => setS({ ...s, summary_channel: e.target.value })}><option value="sms">SMS</option><option value="whatsapp">WhatsApp</option><option value="none">Off</option></select></label>
        {field("display_name", "Business name shown in the app")}
        <h2>When to warn me</h2>
        {field("loss_threshold_litres", "Fuel loss above (litres)")}{field("loss_threshold_pct", "or above (% of fuel sold)")}
        {field("cash_variance_amount", "Cash difference above (amount)")}{field("cash_variance_pct", "or above (% of sales)")}
        {field("low_stock_days", "Low stock when days left is")}{field("refill_lead_days", "Order this many days before empty")}
        <button className="btn primary" onClick={wrap(() => api("/api/settings/", { method: "PATCH", body: { ...s, logo_url: undefined } }), "Saved.")}>Save settings</button></section>}
      <section className="card form">{owner && <h2>Team</h2>}
        {users.map((u) => (
          <div key={u.id} className="member">
          <div className="row between gap member-head"><span>{u.full_name}<br /><span className="muted small">{u.role} · {u.phone ?? u.email}</span></span>
            {owner && editing !== u.id && <button type="button" className="link" onClick={() => setEditing(u.id)}>Edit</button>}
          </div>
            {/* managers: tick which attendants work at their own station(s); the owner places everyone */}
            {!owner && u.role === "attendant" && stations.map((st) => (
              <label key={st.id} className="row gap"><input type="checkbox" aria-label={`${u.full_name} works at ${st.name}`}
                checked={!u.station_ids?.length || u.station_ids.includes(st.id)}
                onChange={(e) => void wrap(() => api(`/api/users/${u.id}/stations/`, { method: "PUT", body: {
                  station_ids: stations.filter((x) => (x.id === st.id ? e.target.checked : !u.station_ids?.length || u.station_ids.includes(x.id))).map((x) => x.id) } }),
                `${u.full_name}: ${e.target.checked ? "added to" : "taken off"} ${st.name}.`)()} />{st.name}</label>
            ))}
            {owner && (u.role === "manager" || u.role === "attendant") && (
              <select aria-label={`Station of ${u.full_name}`} value={u.station_ids?.length === 1 ? u.station_ids[0] : ""}
                onChange={(e) => void wrap(() => setStation(u, e.target.value), `${u.full_name}: station saved.`)()}>
                <option value="">{u.role === "manager" ? "No station yet" : u.station_ids?.length > 1 ? "Several stations" : "All stations"}</option>
                {stations.map((st) => {
                  const other = u.role === "manager" ? managerOf(st.id) : undefined;
                  return <option key={st.id} value={st.id} disabled={Boolean(other && other.id !== u.id)}>{st.name}{other && other.id !== u.id ? ` (managed by ${other.full_name})` : ""}</option>;
                })}
              </select>
            )}
          {owner && editing === u.id && (
            <MemberEditor member={u} self={u.id === user?.id} onClose={() => setEditing(null)}
              onSaved={(text) => { setEditing(null); setMsg(text); setError(null); void load(); }} />
          )}
          </div>
        ))}
        <label>Name<input value={nu.full_name} onChange={(e) => setNu({ ...nu, full_name: e.target.value })} /></label>
        <label>Phone<input inputMode="tel" value={nu.phone} onChange={(e) => setNu({ ...nu, phone: e.target.value })} /></label>
        {owner && <label>Role<select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}><option value="attendant">Attendant</option><option value="manager">Manager</option></select></label>}
        <label>Password<PasswordInput value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} autoComplete="new-password" /></label>
        {owner ? <label>Station<select value={nu.station} onChange={(e) => setNu({ ...nu, station: e.target.value })}>
          <option value="">{nu.role === "manager" ? "Choose later" : "All stations"}</option>
          {stations.map((st) => <option key={st.id} value={st.id} disabled={nu.role === "manager" && Boolean(managerOf(st.id))}>{st.name}{nu.role === "manager" && managerOf(st.id) ? ` (managed by ${managerOf(st.id).full_name})` : ""}</option>)}
        </select></label>
          : <p className="muted">A new attendant works at your station. They choose their own password when they first sign in. Only the owner changes names, phone numbers or passwords.</p>}
        <button className="btn" onClick={wrap(async () => {
          const { station, ...person } = nu;
          const created = await api<any>("/api/users/", { method: "POST", body: { ...person, phone: person.phone || null } });
          if (owner && station) await setStation(created, station);
          setNu({ full_name: "", phone: "", role: "attendant", password: "", station: "" });
        }, "User added.")}>{owner ? "Add user" : "Add attendant"}</button></section>
      <section className="card form"><h2>Your account</h2>
        <p className="muted">Signed in as {user?.full_name}{user?.phone || user?.email ? ` (${user?.phone ?? user?.email})` : ""}.</p>
        <Link className="btn" to="/change-password">Change my password</Link></section>
      {owner && <section className="card form"><h2>Your data</h2><p className="muted">Download everything we hold for your business (CSV and JSON). The link works for 24 hours.</p>
        <button className="btn" onClick={wrap(() => api("/api/exports/", { method: "POST" }), "Preparing your export. Refresh in a minute.")}>Export my data</button>
        {exports.map((e) => <div key={e.id} className="row between"><span>{new Date(e.created_at).toLocaleString()} · {e.status}</span>{e.status === "ready" && <button className="btn" onClick={() => void download(e.id)}>Download</button>}</div>)}</section>}
    </>
  );
}

/** Owner: change a team member's details, set a new password for them (they choose their own at their next sign-in),
 * or remove them. The owner changes their own password under "Your account", with their current password. */
function MemberEditor({ member, self, onClose, onSaved }: { member: any; self: boolean; onClose: () => void; onSaved: (msg: string) => void }) {
  const [form, setForm] = useState({ full_name: member.full_name ?? "", phone: member.phone ?? "", email: member.email ?? "", role: member.role, password: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: any) => setForm({ ...form, [k]: e.target.value });
  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true); setError(null);
    try { await fn(); onSaved(msg); } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };
  const save = () => run(() => api(`/api/users/${member.id}/`, { method: "PATCH", body: {
    full_name: form.full_name, phone: form.phone || null, email: form.email || null,
    ...(self ? {} : { role: form.role }), ...(form.password ? { password: form.password } : {}),
  } }), form.password ? `${form.full_name}: saved. They choose their own password at their next sign-in.` : `${form.full_name}: saved.`);
  const remove = () => {
    if (!window.confirm(`Remove ${member.full_name}? They can no longer sign in. Their past shifts stay.`)) return;
    void run(() => api(`/api/users/${member.id}/`, { method: "DELETE" }), `${member.full_name} removed.`);
  };
  return (
    <div className="card form" aria-label={`Edit ${member.full_name}`} role="group">
      <label>Name<input value={form.full_name} onChange={set("full_name")} /></label>
      <label>Phone<input inputMode="tel" value={form.phone} onChange={set("phone")} /></label>
      <label>Email<input type="email" value={form.email} onChange={set("email")} /></label>
      {!self && member.role !== "owner" && (
        <label>Role<select value={form.role} onChange={set("role")}><option value="attendant">Attendant</option><option value="manager">Manager</option></select></label>
      )}
      {!self && (
        <label>New password (leave empty to keep theirs)<PasswordInput value={form.password} onChange={set("password")} autoComplete="new-password" /></label>
      )}
      {self && <p className="muted">To change your own password, use "Change my password" under Your account.</p>}
      <ErrorNote message={error} />
      <div className="row gap">
        <button type="button" className="btn primary" disabled={busy} onClick={() => void save()}>Save</button>
        <button type="button" className="link" onClick={onClose}>Cancel</button>
        {!self && member.role !== "owner" && <button type="button" className="btn danger" disabled={busy} onClick={remove}>Remove</button>}
      </div>
    </div>
  );
}
