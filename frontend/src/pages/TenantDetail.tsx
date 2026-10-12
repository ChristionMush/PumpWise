import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, CredentialsCard, ErrorNote } from "../components";
import type { Credentials, Subscription, TenantSummary } from "../types";

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

      <section className="card">
        <h2>Stations ({tenant.station_count})</h2>
        <ul className="plain">
          {tenant.stations?.map((s) => <li key={s.id}><strong>{s.name}</strong>{s.address && <span className="muted"> · {s.address}</span>}</li>)}
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
