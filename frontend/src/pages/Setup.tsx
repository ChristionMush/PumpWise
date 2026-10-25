import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";
import { CalibrationEditor } from "./Calibration";

/** A pump has at least one nozzle; more can be added. Each draws from a tank at the pump's station. */
const nozzleProblem = (tanks: string[]) => (tanks.length < 1 ? "A pump has at least one nozzle." : null);
const pumpProblem = (p: any) => (nozzleProblem(p.nozzles.map((n: any) => n.tank)) ? "needs a nozzle" : null);

export default function Setup() {
  const { user } = useAuth();
  const [stations, setStations] = useState<any[]>([]);
  const [fuels, setFuels] = useState<any[]>([]);
  const [tanks, setTanks] = useState<any[]>([]);
  const [pumps, setPumps] = useState<any[]>([]);
  const [check, setCheck] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [f, setF] = useState<Record<string, string>>({});
  const [ptanks, setPtanks] = useState<string[]>([""]); // one tank per nozzle of the new pump
  const [calibTank, setCalibTank] = useState<string | null>(null); // the tank whose chart is being loaded
  const [note, setNote] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null); // "tank:<id>", "pump:<id>" or "nozzle:<id>" (owner only)
  const owner = user?.role === "owner";
  const saved = (msg: string) => { setEditing(null); setNote(msg); setError(null); void load(); };
  const load = async () => {
    const [s, fu, t, p, c] = await Promise.all([api("/api/stations/"), api("/api/fuel-types/"), api("/api/tanks/"), api("/api/pumps/"), api("/api/onboarding/checklist/")]);
    setStations(s.results); setFuels(fu.results); setTanks(t.results); setPumps(p.results); setCheck(c);
  };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  const post = (path: string, body: any, guard?: () => string | null) => async (e: FormEvent) => {
    e.preventDefault();
    const problem = guard?.();
    if (problem) return setError(problem);
    try { await api(path, { method: "POST", body }); setF({}); setPtanks([""]); setError(null); await load(); } catch (err) { setError(errorMessage(err)); }
  };
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const stationTanks = tanks.filter((t) => t.station === f.pstation);
  const nozzlePump = pumps.find((p) => p.id === f.npump);
  const setTank = (i: number, v: string) => setPtanks(ptanks.map((t, j) => (j === i ? v : t)));
  const tankName = (id: string) => tanks.find((t) => t.id === id);
  return (
    <>
      <h1>Setup</h1><ErrorNote message={error} />
      {check && !check.ready && (
        <section className="card"><h2>Getting ready: {check.percent}%</h2><div className="bar"><div className="fill" style={{ width: `${check.percent}%` }} /></div>
          <ul className="plain">{check.items.map((i: any) => <li key={i.key}>{i.done ? "✅" : "⬜"} {i.title}{!i.done && <span className="muted"> · {i.detail}</span>}</li>)}</ul></section>
      )}
      {check?.ready && <p className="ok-note">✅ Everything is set up. You are ready to go live.</p>}
      {note && <p role="status" className="ok-note">{note}</p>}
      {tanks.length > 0 && (
        <section className="card form" id="calibration"><h2>Tank calibration charts (dip charts)</h2>
          <p className="muted">A tank's chart turns a dip height in millimetres into litres. Load it once per tank, from the chart the tank supplier gave you.</p>
          {tanks.map((t) => (
            <div key={t.id} className="calib-row">
              <div className="row between gap">
                <span><strong>{t.name}</strong> <span className="muted">· {stations.find((s) => s.id === t.station)?.name} · {t.fuel_code}</span></span>
                {t.has_calibration ? <span className="badge badge-active">Chart loaded</span> : <span className="badge badge-suspended">Missing</span>}
              </div>
              {calibTank === t.id
                ? <CalibrationEditor tank={t} onClose={() => setCalibTank(null)} onSaved={() => { setCalibTank(null); setNote(`Chart saved for ${t.name}. Dips can now be taken in millimetres.`); void load(); }} />
                : <button type="button" className="btn" onClick={() => { setCalibTank(t.id); setNote(null); }}>{t.has_calibration ? "Replace chart" : "Upload chart"}</button>}
            </div>
          ))}
        </section>
      )}
      {stations.map((s) => (
        <section key={s.id} className="card"><h2>{s.name}</h2>
          {s.assignment_mode === "assigned" && <p className="muted">Assigned pumps: each attendant works the pumps a manager gives them (Pumps screen).</p>}
          {tanks.filter((t) => t.station === s.id).map((t) => (
            <div key={t.id}>
              <p>🛢 {t.name} · {t.fuel_code} · {Number(t.capacity_litres).toLocaleString()} L · reserve {Number(t.reserve_litres).toLocaleString()} L {!t.has_calibration && <a className="warn" href="#calibration" onClick={() => setCalibTank(t.id)}>needs calibration chart: upload it</a>}
                {owner && editing !== `tank:${t.id}` && <button type="button" className="link" onClick={() => setEditing(`tank:${t.id}`)}>Edit</button>}</p>
              {editing === `tank:${t.id}` && (
                <ItemEditor title={`Edit ${t.name}`} path={`/api/tanks/${t.id}/`} onClose={() => setEditing(null)} onSaved={() => saved(`${t.name} saved.`)}
                  note="The fuel and station of a tank are fixed: they decide past sales. Add a new tank if they are wrong."
                  fields={[{ key: "name", label: "Name", value: t.name }, { key: "capacity_litres", label: "Capacity (litres)", value: t.capacity_litres, numeric: true },
                    { key: "reserve_litres", label: "Reserve level (litres)", value: t.reserve_litres, numeric: true }]} />
              )}
            </div>
          ))}
          {pumps.filter((p) => p.station === s.id).map((p) => (
            <div key={p.id}>
              <p>⛽ {p.name} · {p.nozzles.map((n: any) => `${n.label || `Nozzle ${n.number}`}: ${tankName(n.tank)?.name ?? n.tank_name} (${n.fuel_code})`).join(" · ")}
                {p.assigned_to_name && <span className="muted"> · {p.assigned_to_name}</span>}
                {p.brand && <span className="muted"> · {p.brand} {p.model}</span>}
                {pumpProblem(p) && <span className="warn"> {pumpProblem(p)}</span>}
                {owner && editing !== `pump:${p.id}` && <button type="button" className="link" onClick={() => setEditing(`pump:${p.id}`)}>Edit</button>}</p>
              {editing === `pump:${p.id}` && (
                <ItemEditor title={`Edit ${p.name}`} path={`/api/pumps/${p.id}/`} onClose={() => setEditing(null)} onSaved={() => saved(`${p.name} saved.`)}
                  fields={[{ key: "name", label: "Name", value: p.name }, { key: "brand", label: "Brand", value: p.brand ?? "" },
                    { key: "model", label: "Model", value: p.model ?? "" }, { key: "year", label: "Year", value: p.year ?? "", numeric: true }]}>
                  {p.nozzles.map((n: any) => (
                    <div key={n.id} className="row gap">
                      <span>{n.label || `Nozzle ${n.number}`}: {tankName(n.tank)?.name ?? n.tank_name} ({n.fuel_code})</span>
                      {editing !== `nozzle:${n.id}` && <button type="button" className="link" onClick={() => setEditing(`nozzle:${n.id}`)}>Edit nozzle</button>}
                    </div>
                  ))}
                </ItemEditor>
              )}
              {p.nozzles.filter((n: any) => editing === `nozzle:${n.id}`).map((n: any) => (
                <ItemEditor key={n.id} title={`Edit ${p.name}, ${n.label || `nozzle ${n.number}`}`} path={`/api/nozzles/${n.id}/`} onClose={() => setEditing(null)}
                  onSaved={() => saved(`${p.name}, ${n.label || `nozzle ${n.number}`} saved.`)}
                  note="Change the tank only if the nozzle was set up on the wrong one: its sales are counted against the tank it draws from."
                  fields={[{ key: "label", label: "Label", value: n.label ?? "" },
                    { key: "tank", label: "Draws from", value: n.tank, options: tanks.filter((x) => x.station === p.station).map((x) => ({ value: x.id, label: `${x.name} · ${x.fuel_code}` })) }]} />
              ))}
            </div>
          ))}
        </section>
      ))}
      {user?.role === "owner" && ( /* only the owner adds stations; each station has one manager (Settings, Team) */
        <section className="card"><h2>Stations</h2><p className="muted">To add a station, contact PumpWise support.</p></section>
      )}
      <form className="card form" onSubmit={post("/api/tanks/", { station: f.tstation, name: f.tname, fuel_type: f.tfuel, capacity_litres: f.tcap, reserve_litres: f.tres || "0" })}><h2>Add tank</h2>
        <label>Station<select required value={f.tstation ?? ""} onChange={set("tstation")}><option value="" />{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label>Name<input required value={f.tname ?? ""} onChange={set("tname")} /></label>
        <label>Fuel<select required value={f.tfuel ?? ""} onChange={set("tfuel")}><option value="" />{fuels.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
        <label>Capacity (litres)<input required inputMode="decimal" value={f.tcap ?? ""} onChange={set("tcap")} /></label>
        <label>Reserve level (litres)<input inputMode="decimal" value={f.tres ?? ""} onChange={set("tres")} /></label><button className="btn">Add tank</button></form>
      <form className="card form" onSubmit={post("/api/pumps/", {
        station: f.pstation, name: f.pname, brand: f.pbrand ?? "", model: f.pmodel ?? "", ...(f.pyear ? { year: Number(f.pyear) } : {}),
        nozzles: ptanks.map((tank) => ({ tank })),
      }, () => nozzleProblem(ptanks.filter(Boolean)))}><h2>Add pump</h2>
        <p className="muted">A pump has at least one nozzle. Add more nozzles if the pump has them.</p>
        <label>Station<select required value={f.pstation ?? ""} onChange={(e) => { setF({ ...f, pstation: e.target.value }); setPtanks(ptanks.map(() => "")); }}><option value="" />{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label>Name<input required value={f.pname ?? ""} onChange={set("pname")} /></label>
        {ptanks.map((t, i) => (
          <div key={i} className="row gap">
            <label>Nozzle {i + 1} draws from<select required value={t} onChange={(e) => setTank(i, e.target.value)}><option value="" />{stationTanks.map((x) => <option key={x.id} value={x.id}>{x.name} · {x.fuel_code}</option>)}</select></label>
            {ptanks.length > 1 && <button type="button" className="link" onClick={() => setPtanks(ptanks.filter((_, j) => j !== i))}>Remove nozzle {i + 1}</button>}
          </div>
        ))}
        <button type="button" className="btn" onClick={() => setPtanks([...ptanks, ""])}>+ Add nozzle</button>
        {f.pstation && stationTanks.length < 1 && <p className="warn">Add a tank at this station first: each nozzle draws from a tank.</p>}
        <label>Brand (optional)<input value={f.pbrand ?? ""} onChange={set("pbrand")} /></label>
        <label>Model (optional)<input value={f.pmodel ?? ""} onChange={set("pmodel")} /></label>
        <label>Year (optional)<input inputMode="numeric" value={f.pyear ?? ""} onChange={set("pyear")} /></label><button className="btn">Add pump</button></form>
      <form className="card form" onSubmit={post("/api/nozzles/", { pump: f.npump, tank: f.ntank, label: f.nlabel ?? "" })}><h2>Add a nozzle to a pump</h2>
        <label>Pump<select required value={f.npump ?? ""} onChange={(e) => setF({ ...f, npump: e.target.value, ntank: "" })}><option value="" />{pumps.map((p) => <option key={p.id} value={p.id}>{stations.find((s) => s.id === p.station)?.name} · {p.name}</option>)}</select></label>
        <label>Draws from<select required value={f.ntank ?? ""} onChange={set("ntank")}><option value="" />{tanks.filter((t) => t.station === nozzlePump?.station).map((t) => <option key={t.id} value={t.id}>{t.name} · {t.fuel_code}</option>)}</select></label>
        <label>Label (optional)<input value={f.nlabel ?? ""} onChange={set("nlabel")} /></label><button className="btn">Add nozzle</button></form>
    </>
  );
}

type Field = { key: string; label: string; value: string | number; numeric?: boolean; options?: { value: string; label: string }[] };

/** Owner: edit a tank, pump or nozzle in place. Only changed fields are sent; every change is in the audit log. */
function ItemEditor({ title, path, fields, note, onClose, onSaved, children }: {
  title: string; path: string; fields: Field[]; note?: string; onClose: () => void; onSaved: () => void; children?: React.ReactNode;
}) {
  const [values, setValues] = useState<Record<string, string>>(Object.fromEntries(fields.map((f) => [f.key, String(f.value ?? "")])));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    for (const f of fields) {
      if (values[f.key] === String(f.value ?? "")) continue;
      body[f.key] = f.numeric ? (values[f.key] === "" ? null : values[f.key].replace(",", ".")) : values[f.key];
    }
    if (!Object.keys(body).length) return onClose();
    setBusy(true); setError(null);
    try { await api(path, { method: "PATCH", body }); onSaved(); } catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  };
  return (
    <form className="card form" onSubmit={save} aria-label={title}>
      <h3>{title}</h3>
      {fields.map((f) => (
        <label key={f.key}>{f.label}
          {f.options
            ? <select value={values[f.key]} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}>{f.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select>
            : <input inputMode={f.numeric ? "decimal" : undefined} value={values[f.key]} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />}
        </label>
      ))}
      {note && <p className="muted">{note}</p>}
      {children}
      <ErrorNote message={error} />
      <div className="row gap"><button className="btn primary" disabled={busy}>Save</button><button type="button" className="link" onClick={onClose}>Cancel</button></div>
    </form>
  );
}
