import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";

/** A pump has at least two nozzles, drawing from at least two separate tanks (some pumps have more nozzles). */
function nozzleProblem(tanks: string[]) {
  if (tanks.length < 2) return "A pump has at least two nozzles.";
  if (new Set(tanks).size < 2) return "A pump's nozzles must draw from at least two separate tanks.";
  return null;
}
const pumpProblem = (p: any) => (nozzleProblem(p.nozzles.map((n: any) => n.tank)) ? "needs nozzles on two separate tanks" : null);

export default function Setup() {
  const { user } = useAuth();
  const [stations, setStations] = useState<any[]>([]);
  const [fuels, setFuels] = useState<any[]>([]);
  const [tanks, setTanks] = useState<any[]>([]);
  const [pumps, setPumps] = useState<any[]>([]);
  const [check, setCheck] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [f, setF] = useState<Record<string, string>>({});
  const [ptanks, setPtanks] = useState<string[]>(["", ""]); // one tank per nozzle of the new pump
  const load = async () => {
    const [s, fu, t, p, c] = await Promise.all([api("/api/stations/"), api("/api/fuel-types/"), api("/api/tanks/"), api("/api/pumps/"), api("/api/onboarding/checklist/")]);
    setStations(s.results); setFuels(fu.results); setTanks(t.results); setPumps(p.results); setCheck(c);
  };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  const post = (path: string, body: any, guard?: () => string | null) => async (e: FormEvent) => {
    e.preventDefault();
    const problem = guard?.();
    if (problem) return setError(problem);
    try { await api(path, { method: "POST", body }); setF({}); setPtanks(["", ""]); setError(null); await load(); } catch (err) { setError(errorMessage(err)); }
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
      {stations.map((s) => (
        <section key={s.id} className="card"><h2>{s.name}</h2>
          {s.assignment_mode === "assigned" && <p className="muted">Assigned pumps: each attendant works the pumps a manager gives them (Pumps screen).</p>}
          {tanks.filter((t) => t.station === s.id).map((t) => <p key={t.id}>🛢 {t.name} · {t.fuel_code} · {Number(t.capacity_litres).toLocaleString()} L {!t.has_calibration && <span className="warn">needs calibration chart</span>}</p>)}
          {pumps.filter((p) => p.station === s.id).map((p) => (
            <p key={p.id}>⛽ {p.name} · {p.nozzles.map((n: any) => `${n.label || `Nozzle ${n.number}`}: ${tankName(n.tank)?.name ?? n.tank_name} (${n.fuel_code})`).join(" · ")}
              {p.assigned_to_name && <span className="muted"> · {p.assigned_to_name}</span>}
              {p.brand && <span className="muted"> · {p.brand} {p.model}</span>}
              {pumpProblem(p) && <span className="warn"> {pumpProblem(p)}</span>}</p>
          ))}
        </section>
      ))}
      {user?.role === "owner" && ( /* only the owner adds stations; each station has one manager (Settings, Team) */
        <form className="card form" onSubmit={post("/api/stations/", { name: f.sname })}><h2>Add station</h2><label>Name<input required value={f.sname ?? ""} onChange={set("sname")} /></label><button className="btn">Add station</button></form>
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
        <p className="muted">A pump has at least two nozzles, drawing from at least two separate tanks. Add more nozzles if the pump has them.</p>
        <label>Station<select required value={f.pstation ?? ""} onChange={(e) => { setF({ ...f, pstation: e.target.value }); setPtanks(ptanks.map(() => "")); }}><option value="" />{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label>Name<input required value={f.pname ?? ""} onChange={set("pname")} /></label>
        {ptanks.map((t, i) => (
          <div key={i} className="row gap">
            <label>Nozzle {i + 1} draws from<select required value={t} onChange={(e) => setTank(i, e.target.value)}><option value="" />{stationTanks.map((x) => <option key={x.id} value={x.id}>{x.name} · {x.fuel_code}</option>)}</select></label>
            {ptanks.length > 2 && <button type="button" className="link" onClick={() => setPtanks(ptanks.filter((_, j) => j !== i))}>Remove nozzle {i + 1}</button>}
          </div>
        ))}
        <button type="button" className="btn" onClick={() => setPtanks([...ptanks, ""])}>+ Add nozzle</button>
        {f.pstation && stationTanks.length < 2 && <p className="warn">Add a second tank at this station first: a pump's nozzles draw from at least two separate tanks.</p>}
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
