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
  const [expenses, setExpenses] = useState(false);
  const [shiftOpts, setShiftOpts] = useState({ attendants_pick_pumps: false, dip_at_shift_end: false, close_day_at_change: "", pooled_tanks: true });
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
        expenses_enabled: expenses,
        ...shiftOpts,
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
        <h2>Expenses</h2>
        <label className="row gap"><input type="checkbox" checked={expenses} onChange={(e) => setExpenses(e.target.checked)} /> Expenses</label>
        <p className="muted">Attendants and managers record what they pay out (lunch, generator fuel...). Recorded only: it does not change the cash handed in.</p>
      </section>
      <section className="card form">
        <h2>Tanks and shift change</h2>
        <label className="row gap"><input type="checkbox" checked={shiftOpts.pooled_tanks} onChange={(e) => setShiftOpts({ ...shiftOpts, pooled_tanks: e.target.checked })} /> Pumps draw from every tank of their fuel</label>
        <p className="muted">E.g. two petrol tanks: every petrol pump draws from both, and stock and the fuel check count them as one. Off: each tank is its own stock.</p>
        <label className="row gap"><input type="checkbox" checked={shiftOpts.attendants_pick_pumps} onChange={(e) => setShiftOpts({ ...shiftOpts, attendants_pick_pumps: e.target.checked })} /> Attendants pick their pumps</label>
        <p className="muted">Assigned pumps: each attendant ticks the free pumps they take when they start, instead of a manager allocating them.</p>
        <label className="row gap"><input type="checkbox" checked={shiftOpts.dip_at_shift_end} onChange={(e) => setShiftOpts({ ...shiftOpts, dip_at_shift_end: e.target.checked })} /> Dip at the end of a shift</label>
        <p className="muted">Ending a shift asks the outgoing attendant to dip every tank (the first to end; not again within the hour).</p>
        <label>Close the day at a shift change<select value={shiftOpts.close_day_at_change} onChange={(e) => setShiftOpts({ ...shiftOpts, close_day_at_change: e.target.value })}>
          <option value="">No (closed on the Dip screen)</option><option value="07:00">07:00 change</option><option value="18:00">18:00 change</option></select></label>
        <p className="muted">Once everyone from the outgoing shift has ended and every tank was dipped, the day closes with those dips and every pump's last number.</p>
      </section>
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
