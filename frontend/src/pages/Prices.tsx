import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";

export default function Prices() {
  const [stations, setStations] = useState<any[]>([]);
  const [fuels, setFuels] = useState<any[]>([]);
  const [prices, setPrices] = useState<any[]>([]);
  const [edit, setEdit] = useState<Record<string, { price: string; at: string }>>({});
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const load = async () => {
    const [s, f, p] = await Promise.all([api("/api/stations/"), api("/api/fuel-types/"), api("/api/prices/")]);
    setStations(s.results); setFuels(f.results); setPrices(p);
  };
  useEffect(() => { void load().catch((e) => setError(errorMessage(e))); }, []);
  const current = (st: string, fuel: string) => prices.find((p) => p.station === st && p.fuel_type === fuel && !p.effective_to);
  const apply = async (st: any, f: any) => {
    const key = st.id + f.id, e = edit[key];
    if (!e?.price) return;
    try {
      await api("/api/prices/", { method: "POST", body: { station: st.id, fuel_type: f.id, price_per_litre: e.price, ...(e.at ? { effective_from: new Date(e.at).toISOString() } : {}) } });
      setEdit({ ...edit, [key]: { price: "", at: "" } }); setOk(`${f.name} price updated at ${st.name}.`); setError(null); await load();
    } catch (err) { setError(errorMessage(err)); }
  };
  return (
    <>
      <h1>Fuel prices</h1><p className="muted">A new price applies from the time you choose (default: now). Old prices are kept, so past sales never change.</p>
      <ErrorNote message={error} />{ok && <p role="status" className="ok-note">{ok}</p>}
      {stations.map((st) => (
        <section key={st.id} className="card form"><h2>{st.name} <span className="muted">({st.pricing_currency})</span></h2>
          {fuels.map((f) => { const cur = current(st.id, f.id), key = st.id + f.id; return (
            <div key={f.id} className="price-row"><strong>{f.name}</strong><span className="muted">now {cur ? Number(cur.price_per_litre).toFixed(2) : "not set"}{cur?.set_by && ` · set by ${cur.set_by}, ${new Date(cur.created_at).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`}</span>
              <input aria-label={`New ${f.name} price at ${st.name}`} inputMode="decimal" placeholder="New price" value={edit[key]?.price ?? ""} onChange={(e) => setEdit({ ...edit, [key]: { price: e.target.value, at: edit[key]?.at ?? "" } })} />
              <input aria-label="From" type="datetime-local" value={edit[key]?.at ?? ""} onChange={(e) => setEdit({ ...edit, [key]: { price: edit[key]?.price ?? "", at: e.target.value } })} />
              <button className="btn primary" onClick={() => void apply(st, f)}>Update</button></div>); })}
        </section>
      ))}
    </>
  );
}
