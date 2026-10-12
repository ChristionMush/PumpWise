import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";

export default function Setup() {
  const [stations, setStations] = useState<any[]>([]);
  const [fuels, setFuels] = useState<any[]>([]);
  const [tanks, setTanks] = useState<any[]>([]);
  const [pumps, setPumps] = useState<any[]>([]);
  const [check, setCheck] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [f, setF] = useState<Record<string, string>>({});
  const load = async () => {
    const [s, fu, t, p, c] = await Promise.all([api("/api/stations/"), api("/api/fuel-types/"), api("/api/tanks/"), api("/api/pumps/"), api("/api/onboarding/checklist/")]);
    setStations(s.results); setFuels(fu.results); setTanks(t.results); setPumps(p.results); setCheck(c);
  };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  const post = (path: string, body: any) => async (e: FormEvent) => {
    e.preventDefault();
    try { await api(path, { method: "POST", body }); setF({}); setError(null); await load(); } catch (err) { setError(errorMessage(err)); }
  };
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const tick = async () => { await api("/api/onboarding/flags/", { method: "POST", body: { key: "qr_printed", value: true } }); await load(); };
  return (
    <>
      <h1>Setup</h1><ErrorNote message={error} />
      {check && !check.ready && (
        <section className="card"><h2>Getting ready: {check.percent}%</h2><div className="bar"><div className="fill" style={{ width: `${check.percent}%` }} /></div>
          <ul className="plain">{check.items.map((i: any) => <li key={i.key}>{i.done ? "✅" : "⬜"} {i.title}{!i.done && <span className="muted"> · {i.detail}</span>}</li>)}</ul>
          {!check.items.find((i: any) => i.key === "qr")?.done && <button className="btn" onClick={() => void tick()}>I have printed the QR codes</button>}</section>
      )}
      {check?.ready && <p className="ok-note">✅ Everything is set up. You are ready to go live.</p>}
      {stations.map((s) => (
        <section key={s.id} className="card"><h2>{s.name}</h2>
          {tanks.filter((t) => t.station === s.id).map((t) => <p key={t.id}>🛢 {t.name} · {t.fuel_code} · {Number(t.capacity_litres).toLocaleString()} L {!t.has_calibration && <span className="warn">needs calibration chart</span>} <a href={`/api/qr-image/tanks/${t.id}/`} target="_blank" rel="noreferrer">QR</a></p>)}
          {pumps.filter((p) => p.station === s.id).map((p) => <p key={p.id}>⛽ {p.name} · {p.nozzles.length} nozzle{p.nozzles.length === 1 ? "" : "s"} {p.brand && <span className="muted">· {p.brand} {p.model}</span>} <a href={`/api/qr-image/pumps/${p.id}/`} target="_blank" rel="noreferrer">QR</a></p>)}
        </section>
      ))}
      <form className="card form" onSubmit={post("/api/stations/", { name: f.sname })}><h2>Add station</h2><label>Name<input required value={f.sname ?? ""} onChange={set("sname")} /></label><button className="btn">Add station</button></form>
      <form className="card form" onSubmit={post("/api/tanks/", { station: f.tstation, name: f.tname, fuel_type: f.tfuel, capacity_litres: f.tcap, reserve_litres: f.tres || "0" })}><h2>Add tank</h2>
        <label>Station<select required value={f.tstation ?? ""} onChange={set("tstation")}><option value="" />{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label>Name<input required value={f.tname ?? ""} onChange={set("tname")} /></label>
        <label>Fuel<select required value={f.tfuel ?? ""} onChange={set("tfuel")}><option value="" />{fuels.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
        <label>Capacity (litres)<input required inputMode="decimal" value={f.tcap ?? ""} onChange={set("tcap")} /></label>
        <label>Reserve level (litres)<input inputMode="decimal" value={f.tres ?? ""} onChange={set("tres")} /></label><button className="btn">Add tank</button></form>
      <form className="card form" onSubmit={post("/api/pumps/", { station: tanks.find((t) => t.id === f.ptank)?.station, tank: f.ptank, name: f.pname, brand: f.pbrand ?? "", model: f.pmodel ?? "", ...(f.pyear ? { year: Number(f.pyear) } : {}) })}><h2>Add pump</h2>
        <label>Tank<select required value={f.ptank ?? ""} onChange={set("ptank")}><option value="" />{tanks.map((t) => <option key={t.id} value={t.id}>{stations.find((s) => s.id === t.station)?.name} · {t.name}</option>)}</select></label>
        <label>Name<input required value={f.pname ?? ""} onChange={set("pname")} /></label>
        <label>Brand (optional)<input value={f.pbrand ?? ""} onChange={set("pbrand")} /></label>
        <label>Model (optional)<input value={f.pmodel ?? ""} onChange={set("pmodel")} /></label>
        <label>Year (optional)<input inputMode="numeric" value={f.pyear ?? ""} onChange={set("pyear")} /></label><button className="btn">Add pump</button></form>
      <form className="card form" onSubmit={post("/api/nozzles/", { pump: f.npump, number: Number(f.nnum), label: f.nlabel ?? "" })}><h2>Add nozzle</h2>
        <label>Pump<select required value={f.npump ?? ""} onChange={set("npump")}><option value="" />{pumps.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Number<input required inputMode="numeric" value={f.nnum ?? ""} onChange={set("nnum")} /></label>
        <label>Label (optional)<input value={f.nlabel ?? ""} onChange={set("nlabel")} /></label><button className="btn">Add nozzle</button></form>
    </>
  );
}
