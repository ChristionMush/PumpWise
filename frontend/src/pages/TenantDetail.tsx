import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, CredentialsCard, ErrorNote } from "../components";
import type { AssignmentMode, AttendantsMode, Credentials, PumpSystem, Subscription, TenantSummary } from "../types";

const SUB_STATUSES = ["active", "past_due", "suspended", "cancelled"] as const;

export default function TenantDetail() {
  const { id } = useParams();
  const [tenant, setTenant] = useState<TenantSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const [stationName, setStationName] = useState("");
  const [stationAddress, setStationAddress] = useState("");
  const [sub, setSub] = useState<Subscription | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api<TenantSummary>(`/api/platform/tenants/${id}/`);
      setTenant(data);
      setSub(data.subscription);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  const run = async (fn: () => Promise<void>) => {
    setError(null);
    try { await fn(); await load(); } catch (e) { setError(errorMessage(e)); }
  };

  const setStatus = (status: "active" | "suspended" | "cancelled") => {
    const words = { active: "Reactivate", suspended: "Suspend", cancelled: "Cancel" }[status];
    if (!window.confirm(`${words} ${tenant?.name}?`)) return;
    void run(async () => { await api(`/api/platform/tenants/${id}/`, { method: "PATCH", body: { status } }); });
  };

  const resetOwner = () => {
    if (!window.confirm("Issue a new temporary password for the owner? Their current password stops working.")) return;
    void run(async () => {
      const res = await api<{ owner_credentials: Credentials }>(`/api/platform/tenants/${id}/reset-owner-password/`, { method: "POST" });
      setCredentials(res.owner_credentials);
    });
  };

  const addStation = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      await api(`/api/platform/tenants/${id}/stations/`, { method: "POST", body: { name: stationName, address: stationAddress } });
      setStationName("");
      setStationAddress("");
    });
  };

  // The pump system for the whole business (chosen when it was created).
  const setPumpSystem = (pump_system: PumpSystem) => {
    const label = { shared: "shared pumps with one attendant", shared_multi: "shared pumps with several attendants", assigned: "assigned pumps" }[pump_system];
    if (!window.confirm(`Move every station of ${tenant?.name} to ${label}?`)) return;
    void run(async () => { await api(`/api/platform/tenants/${id}/pump-system/`, { method: "PATCH", body: { pump_system } }); });
  };

  // Paid features (platform only): several attendants per shift, and pump assignment (which needs the first).
  const setFeature = (body: Record<string, boolean | string>) => void run(async () => {
    await api(`/api/platform/tenants/${id}/features/`, { method: "PATCH", body });
  });
  const setPolicy = (stationId: string, body: { attendants_mode?: AttendantsMode; assignment_mode?: AssignmentMode }) => void run(async () => {
    await api(`/api/platform/tenants/${id}/stations/${stationId}/policy/`, { method: "PATCH", body });
  });
  const convertTrial = () => {
    if (!window.confirm(`${tenant?.name} becomes a customer. The demo data is removed; logins and settings stay. Continue?`)) return;
    void run(async () => { await api(`/api/platform/tenants/${id}/convert-trial/`, { method: "POST" }); });
  };

  const saveSub = (e: FormEvent) => {
    e.preventDefault();
    if (!sub) return;
    void run(async () => {
      await api(`/api/platform/tenants/${id}/subscription/`, { method: "PATCH", body: { status: sub.status, plan: sub.plan, paid_through: sub.paid_through || null } });
    });
  };

  if (!tenant) return error ? <ErrorNote message={error} /> : <p className="muted center">Loading…</p>;

  return (
    <>
      <div className="row between">
        <h1>{tenant.name}</h1>
        <Badge kind={tenant.status}>{tenant.status}</Badge>
      </div>
      <ErrorNote message={error} />
      {tenant.is_demo && (
        <section className="card">
          <h2>{tenant.trial_expires_at ? "Trial" : "Demo business"}</h2>
          <p className="muted">{tenant.trial_expires_at ? `Trial until ${tenant.trial_expires_at}. Suspended when it ends; removed 30 days later unless converted.` : "Reloaded with fresh demo data every night."} It never sends real messages and only uses the payment sandbox.</p>
          {tenant.trial_expires_at && <button className="btn primary" onClick={convertTrial}>Convert to customer</button>}
        </section>
      )}

      <section className="card">
        <h2>Access</h2>
        <div className="row gap">
          {tenant.status !== "active" && <button className="btn primary" onClick={() => setStatus("active")}>Reactivate</button>}
          {tenant.status === "active" && <button className="btn danger" onClick={() => setStatus("suspended")}>Suspend</button>}
          {tenant.status !== "cancelled" && <button className="btn" onClick={() => setStatus("cancelled")}>Cancel</button>}
        </div>
        <p className="muted">Suspended: owner and manager are locked out; attendants can still record sales. Cancelled: everyone is locked out. Nothing is deleted.</p>
      </section>

      <section className="card">
        <h2>Owner</h2>
        {tenant.owner ? (
          <p>{tenant.owner.full_name}<br /><span className="muted">{tenant.owner.phone ?? tenant.owner.email}</span></p>
        ) : <p className="muted">No owner found.</p>}
        <button className="btn" onClick={resetOwner}>Reset owner password</button>
      </section>
      {credentials && <CredentialsCard credentials={credentials} title="New temporary password" />}

      <section className="card">
        <h2>Subscription</h2>
        {sub && (
          <form onSubmit={saveSub} className="form">
            <label>Status
              <select value={sub.status} onChange={(e) => setSub({ ...sub, status: e.target.value as Subscription["status"] })}>
                {SUB_STATUSES.map((s) => <option key={s} value={s}>{s.replace("_", " ")}</option>)}
              </select>
            </label>
            <label>Plan<input value={sub.plan} onChange={(e) => setSub({ ...sub, plan: e.target.value })} /></label>
            <label>Paid through<input type="date" value={sub.paid_through ?? ""} onChange={(e) => setSub({ ...sub, paid_through: e.target.value || null })} /></label>
            <button className="btn primary">Save subscription</button>
          </form>
        )}
      </section>

      {tenant.pump_system && (
        <section className="card">
          <h2>Pump system</h2>
          <label>How the pumps are worked
            <select aria-label="Pump system" value={tenant.pump_system} onChange={(e) => setPumpSystem(e.target.value as PumpSystem)}>
              <option value="shared">Shared pumps, one attendant per shift</option>
              <option value="shared_multi">Shared pumps, several attendants: everyone on the shift uses every pump</option>
              <option value="assigned">Assigned pumps: each attendant works their own pumps</option>
            </select>
          </label>
          <p className="muted">New stations follow this. Not possible while a shift is open.</p>
        </section>
      )}

      {tenant.features && (
        <section className="card">
          <h2>Paid features</h2>
          <label className="row gap"><input type="checkbox" checked={tenant.features.multi_attendant_enabled}
            onChange={(e) => setFeature({ multi_attendant_enabled: e.target.checked })} /> Several attendants per shift</label>
          <label className="row gap"><input type="checkbox" checked={tenant.features.pump_assignment_enabled} disabled={!tenant.features.multi_attendant_enabled}
            onChange={(e) => setFeature({ pump_assignment_enabled: e.target.checked })} /> Pump assignment (cash checked per attendant)</label>
          <p className="muted">Removing a feature moves every station back to one attendant and shared pumps. Nothing is deleted. Not possible while a shift is open.</p>
          <label className="row gap"><input type="checkbox" checked={Boolean(tenant.features.expenses_enabled)}
            onChange={(e) => setFeature({ expenses_enabled: e.target.checked })} /> Expenses</label>
          <p className="muted">Attendants and managers record what they pay out (lunch, generator fuel...). Recorded only; off hides the screens and keeps what was recorded.</p>
          <h3>Shift change</h3>
          <label className="row gap"><input type="checkbox" checked={Boolean(tenant.features.attendants_pick_pumps)}
            onChange={(e) => setFeature({ attendants_pick_pumps: e.target.checked })} /> Attendants pick their pumps</label>
          <label className="row gap"><input type="checkbox" checked={Boolean(tenant.features.dip_at_shift_end)}
            onChange={(e) => setFeature({ dip_at_shift_end: e.target.checked })} /> Dip at the end of a shift</label>
          <label>Close the day at a shift change<select value={tenant.features.close_day_at_change ?? ""} onChange={(e) => setFeature({ close_day_at_change: e.target.value })}>
            <option value="">No (closed on the Dip screen)</option><option value="07:00">07:00 change</option><option value="18:00">18:00 change (handover 15:00-20:00)</option></select></label>
          <p className="muted">The dip at the change is taken a few minutes before the last attendant ends, so each day's fuel check can be a few litres off, evened out the next day.</p>
        </section>
      )}

      <section className="card">
        <h2>Stations ({tenant.station_count})</h2>
        <ul className="plain">
          {tenant.stations?.map((s) => (
            <li key={s.id}><strong>{s.name}</strong>{s.address && <span className="muted"> · {s.address}</span>}
              {tenant.features && (
                <div className="row gap">
                  <select aria-label={`${s.name} attendants`} value={s.attendants_mode ?? "single"} onChange={(e) => setPolicy(s.id, { attendants_mode: e.target.value as AttendantsMode })}>
                    <option value="single">One attendant</option>
                    <option value="multiple" disabled={!tenant.features.multi_attendant_enabled}>Several attendants</option>
                  </select>
                  <select aria-label={`${s.name} pumps`} value={s.assignment_mode ?? "shared"} onChange={(e) => setPolicy(s.id, { assignment_mode: e.target.value as AssignmentMode })}>
                    <option value="shared">Shared pumps</option>
                    <option value="assigned" disabled={!tenant.features.pump_assignment_enabled || s.attendants_mode !== "multiple"}>Assigned pumps</option>
                  </select>
                </div>
              )}
              <StationTanks tenantId={id!} stationId={s.id} stationName={s.name} />
            </li>
          ))}
        </ul>
        <form onSubmit={addStation} className="form">
          <label>Station name<input value={stationName} onChange={(e) => setStationName(e.target.value)} required /></label>
          <label>Address (optional)<input value={stationAddress} onChange={(e) => setStationAddress(e.target.value)} /></label>
          <button className="btn">+ Add station</button>
        </form>
      </section>
    </>
  );
}

interface TankRow { id: string; name: string; fuel_code: string; capacity_litres: string; reserve_litres: string; has_calibration: boolean }
interface StationTankInfo {
  tanks: TankRow[]; nozzles: { id: string; pump: string; label: string; tank: string; draws_from?: string[] }[]; fuels: { id: string; name: string; code: string }[];
}

/** Platform: a station's tanks, the tank each pump nozzle draws from, and adding a tank (opens on demand). Owners and
 *  managers do the same on their Setup screen; changing a nozzle's tank waits until no shift is open there. */
function StationTanks({ tenantId, stationId, stationName }: { tenantId: string; stationId: string; stationName: string }) {
  const [info, setInfo] = useState<StationTankInfo | null>(null);
  const [form, setForm] = useState({ name: "", fuel_type: "", capacity_litres: "", reserve_litres: "" });
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const url = `/api/platform/tenants/${tenantId}/stations/${stationId}/tanks/`;
  const load = () => api<StationTankInfo>(url).then((i) => { setInfo(i); setForm((f) => ({ ...f, fuel_type: f.fuel_type || i.fuels[0]?.id || "" })); })
    .catch((e) => setError(errorMessage(e)));
  const run = async (fn: () => Promise<StationTankInfo>, ok: string) => {
    setError(null); setNote(null);
    try { setInfo(await fn()); setNote(ok); return true; } catch (e) { setError(errorMessage(e)); return false; }
  };
  const addTank = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api(url, { method: "POST", body: { ...form, reserve_litres: form.reserve_litres || "0" } }), `${form.name} added to ${stationName}.`)
      .then((saved) => { if (saved) setForm((f) => ({ ...f, name: "", capacity_litres: "", reserve_litres: "" })); }); // a refused tank keeps what was typed
  };
  const setTank = (nozzle: string, tank: string) =>
    void run(() => api(`/api/platform/tenants/${tenantId}/nozzles/${nozzle}/tank/`, { method: "PATCH", body: { tank } }), "Saved.");
  return (
    <details onToggle={(e) => { if ((e.target as HTMLDetailsElement).open && !info) void load(); }}>
      <summary>Tanks</summary>
      {info && (
        <div className="form">
          {info.tanks.length ? (
            <ul className="plain">{info.tanks.map((t) => (
              <li key={t.id}>{t.name} · {t.fuel_code} · {Number(t.capacity_litres).toLocaleString()} L (reserve {Number(t.reserve_litres).toLocaleString()} L)
                {t.has_calibration ? "" : <span className="muted"> · no dip chart yet</span>}</li>
            ))}</ul>
          ) : <p className="muted">No tanks yet.</p>}
          {info.nozzles.length > 0 && info.tanks.length > 0 && (<>
            <h3>Which tank each nozzle draws from</h3>
            {info.nozzles.map((n) => (
              <label key={n.id}>{n.pump} · {n.label}{(n.draws_from?.length ?? 0) > 1 && <span className="muted"> · draws from {n.draws_from!.join(" + ")}</span>}
                <select aria-label={`${stationName} ${n.pump} ${n.label} tank`} value={n.tank} onChange={(e) => setTank(n.id, e.target.value)}>
                  {info.tanks.map((t) => <option key={t.id} value={t.id}>{t.name} ({t.fuel_code})</option>)}
                </select>
              </label>
            ))}
          </>)}
          <form onSubmit={addTank} className="form" aria-label={`Add a tank at ${stationName}`}>
            <h3>Add a tank</h3>
            <label>Tank name<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></label>
            <label>Fuel<select value={form.fuel_type} onChange={(e) => setForm({ ...form, fuel_type: e.target.value })}>
              {info.fuels.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}</select></label>
            <label>Capacity (litres)<input inputMode="decimal" value={form.capacity_litres} onChange={(e) => setForm({ ...form, capacity_litres: e.target.value })} required /></label>
            <label>Reserve (litres, optional)<input inputMode="decimal" value={form.reserve_litres} onChange={(e) => setForm({ ...form, reserve_litres: e.target.value })} /></label>
            <button className="btn">+ Add tank</button>
          </form>
        </div>
      )}
      <ErrorNote message={error} />{note && <p role="status" className="ok-note">{note}</p>}
    </details>
  );
}
