import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { CredentialsCard, ErrorNote } from "../components";
import type { Credentials, PumpSystem, TenantSummary } from "../types";

interface StationRow { name: string; address: string }

export default function CreateTenant() {
  const [businessName, setBusinessName] = useState("");
  const [ownerName, setOwnerName] = useState("");
  const [ownerPhone, setOwnerPhone] = useState("");
  const [ownerEmail, setOwnerEmail] = useState("");
  const [plan, setPlan] = useState("");
  const [paidThrough, setPaidThrough] = useState("");
  const [stations, setStations] = useState<StationRow[]>([{ name: "", address: "" }]);
  const [pumpSystem, setPumpSystem] = useState<PumpSystem | "">(""); // asked up front: no default
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<(TenantSummary & { owner_credentials: Credentials }) | null>(null);

  const setStation = (i: number, patch: Partial<StationRow>) =>
    setStations((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!pumpSystem) return setError("Choose how the pumps are worked: shared or assigned.");
    setBusy(true);
    setError(null);
    try {
      const body = {
        business_name: businessName.trim(),
        owner_name: ownerName.trim(),
        owner_phone: ownerPhone.trim() || null,
        owner_email: ownerEmail.trim() || null,
        plan: plan.trim(),
        paid_through: paidThrough || null,
        stations: stations.filter((s) => s.name.trim()).map((s) => ({ name: s.name.trim(), address: s.address.trim() })),
        pump_system: pumpSystem,
      };
      setCreated(await api("/api/platform/tenants/", { method: "POST", body }));
    } catch (err) {
      setError(errorMessage(err)); // the form keeps what was typed
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    return (
      <>
        <h1>{created.name} created</h1>
        <CredentialsCard credentials={created.owner_credentials} title="Give these to the owner" />
        <Link className="btn primary" to={`/platform/tenants/${created.id}`}>Open business</Link>
      </>
    );
  }

  return (
    <form onSubmit={submit} className="form">
      <h1>New business</h1>
      <section className="card form">
        <h2>Business</h2>
        <label>Business name<input value={businessName} onChange={(e) => setBusinessName(e.target.value)} required /></label>
        <label>Plan (optional)<input value={plan} onChange={(e) => setPlan(e.target.value)} /></label>
        <label>Paid through (optional)<input type="date" value={paidThrough} onChange={(e) => setPaidThrough(e.target.value)} /></label>
      </section>
      <fieldset className="card form">
        <legend><h2>Pump system</h2></legend>
        <p className="muted">How does this business work its pumps?</p>
        <label className="row gap"><input type="radio" name="pump_system" value="shared" checked={pumpSystem === "shared"} onChange={() => setPumpSystem("shared")} />
          Shared pumps, one attendant: a single attendant works every pump on the shift</label>
        <label className="row gap"><input type="radio" name="pump_system" value="shared_multi" checked={pumpSystem === "shared_multi"} onChange={() => setPumpSystem("shared_multi")} />
          Shared pumps, several attendants: everyone on the shift uses every pump</label>
        <label className="row gap"><input type="radio" name="pump_system" value="assigned" checked={pumpSystem === "assigned"} onChange={() => setPumpSystem("assigned")} />
          Assigned pumps: a manager gives each attendant their own pumps</label>
      </fieldset>
      <section className="card form">
        <h2>Owner</h2>
        <label>Owner name<input value={ownerName} onChange={(e) => setOwnerName(e.target.value)} required /></label>
        <label>Owner phone<input type="tel" inputMode="tel" value={ownerPhone} onChange={(e) => setOwnerPhone(e.target.value)} placeholder="0771234567" /></label>
        <label>Owner email<input type="email" value={ownerEmail} onChange={(e) => setOwnerEmail(e.target.value)} /></label>
        <p className="muted">Phone or email is needed. The owner signs in with it.</p>
      </section>
      <section className="card form">
        <h2>Stations</h2>
        {stations.map((s, i) => (
          <div key={i} className="station-row">
            <label>Station name<input value={s.name} onChange={(e) => setStation(i, { name: e.target.value })} /></label>
            <label>Address (optional)<input value={s.address} onChange={(e) => setStation(i, { address: e.target.value })} /></label>
            {stations.length > 1 && (
              <button type="button" className="link" onClick={() => setStations((rows) => rows.filter((_, idx) => idx !== i))}>Remove station</button>
            )}
          </div>
        ))}
        <button type="button" className="btn" onClick={() => setStations((rows) => [...rows, { name: "", address: "" }])}>+ Add another station</button>
      </section>
      <ErrorNote message={error} />
      <button className="btn primary" disabled={busy}>Create business</button>
    </form>
  );
}
