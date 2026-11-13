import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, CredentialsCard, ErrorNote } from "../components";
import type { AssignmentMode, AttendantsMode, Credentials, Modules, Subscription, TenantSummary } from "../types";
import ModulePicker, { changedModules, useCatalog } from "./ModulePicker";

const SUB_STATUSES = ["active", "past_due", "suspended", "cancelled"] as const;

export default function TenantDetail() {
  const { id } = useParams();
  const [tenant, setTenant] = useState<TenantSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const [stationName, setStationName] = useState("");
  const [stationAddress, setStationAddress] = useState("");
  const [sub, setSub] = useState<Subscription | null>(null);
  const catalog = useCatalog();
  const [draft, setDraft] = useState<Modules | null>(null); // the modules as ticked, until saved

  const load = useCallback(async () => {
    try {
      const data = await api<TenantSummary>(`/api/platform/tenants/${id}/`);
      setTenant(data);
      setSub(data.subscription);
      setDraft(data.modules ?? null);
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

  // The business's Mark is its label and starting point: changing it leaves the modules as they are.
  const setMark = (mark: string) => void run(async () => {
    await api(`/api/platform/tenants/${id}/mark/`, { method: "PATCH", body: { mark: mark || null } });
  });
  // Only the modules that changed are sent; the pump allocation waits until no shift is open.
  const saveModules = () => {
    if (!tenant?.modules || !draft || !catalog.modules) return;
    const changed = changedModules(catalog.modules, tenant.modules, draft);
    if (!window.confirm(`Change ${changed.map((m) => m.label).join(", ")} for ${tenant.name}? It takes effect straight away.`)) return;
    void run(async () => {
      await api(`/api/platform/tenants/${id}/modules/`, { method: "PATCH", body: { modules: Object.fromEntries(changed.map((m) => [m.key, draft[m.key]])) } });
    });
  };
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

      {tenant.modules && draft && catalog.modules && catalog.marks && (
        <section className="card form">
          <h2>Mark and modules</h2>
          <label>Mark
            <select value={tenant.mark?.id ?? ""} onChange={(e) => setMark(e.target.value)}>
              <option value="">None</option>
              {catalog.marks.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
            </select>
          </label>
          <p className="muted">The Mark is the way this business started. Changing it leaves the modules below as they are.</p>
          <ModulePicker modules={catalog.modules} marks={catalog.marks} values={draft} onChange={setDraft}
            base={catalog.marks.find((k) => k.id === tenant.mark?.id)} />
          <div className="row gap">
            <button className="btn primary" disabled={!changedModules(catalog.modules, tenant.modules, draft).length} onClick={saveModules}>Save modules</button>
            {changedModules(catalog.modules, tenant.modules, draft).length > 0 && <button className="btn" onClick={() => setDraft(tenant.modules!)}>Undo changes</button>}
          </div>
          <p className="muted">Pump allocation can change only while no shift is open (new stations follow it). Turning a module off hides its screens and keeps what was recorded. Every change goes in the audit log.</p>
        </section>
      )}
      <ErrorNote message={catalog.error} />

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
