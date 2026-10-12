import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorNote } from "../components";
import { enqueue, expectedVsHanded, kvGet, kvSet, uuid, type PriceRow } from "../offline";

export interface Nozzle { id: string; number: number; label: string; meter_max: string; last_reading: string | null }
export interface PumpInfo { id: string; name: string; qr_token: string; fuel_type: string; fuel_code: string; nozzles: Nozzle[] }
export interface StationInfo { id: string; name: string; pricing_currency: string; pumps: PumpInfo[]; tanks: any[]; prices: PriceRow[]; open_shift_id: string | null }
export interface Active { id: string; station_id: string; started_at: string; starts: Record<string, number> }

/** Loads the phone's working data; falls back to the saved copy when there is no signal. */
export async function loadPrefill(): Promise<StationInfo[]> {
  try {
    const res = await api<{ stations: StationInfo[] }>("/api/shifts/prefill/");
    await kvSet("prefill", res.stations);
    return res.stations;
  } catch {
    return (await kvGet<StationInfo[]>("prefill")) ?? [];
  }
}

const TYPES = [["cash", "Cash"], ["mobile_money", "EcoCash / mobile money"], ["card", "Card / swipe"], ["credit", "Credit / coupon"]] as const;
const money = (n: number) => n.toFixed(2);

export default function Shift() {
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [stationId, setStationId] = useState("");
  const [active, setActive] = useState<Active | null>(null);
  const [nums, setNums] = useState<Record<string, string>>({});
  const [cash, setCash] = useState<Record<string, string>>({});
  const [other, setOther] = useState({ currency: "ZWG", amount: "", rate: "" });
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [params] = useSearchParams();
  const navigate = useNavigate();

  useEffect(() => {
    void (async () => {
      const st = await loadPrefill();
      setStations(st);
      const a = await kvGet<Active>("activeShift");
      setActive(a ?? null);
      setStationId(a?.station_id ?? st[0]?.id ?? "");
      if (!a && st[0]) setNums(Object.fromEntries(st[0].pumps.flatMap((p) => p.nozzles.map((n) => [n.id, n.last_reading ?? ""]))));
    })();
  }, []);

  const station = stations.find((s) => s.id === stationId);
  const nozzles = useMemo(() => station?.pumps.flatMap((p) => p.nozzles.map((n) => ({ ...n, pump: p }))) ?? [], [station]);
  const highlight = params.get("pump");

  const start = async () => {
    if (!station) return;
    const readings = nozzles.filter((n) => nums[n.id] !== "" && nums[n.id] !== undefined);
    if (!readings.length) return setError("Enter the meter number for at least one pump.");
    const a: Active = { id: uuid(), station_id: station.id, started_at: new Date().toISOString(), starts: Object.fromEntries(readings.map((n) => [n.id, Number(nums[n.id])])) };
    await kvSet("activeShift", a); // saved on the phone first
    await enqueue("shift_start", { shift_id: a.id, station: station.id, started_at: a.started_at, readings: readings.map((n) => ({ nozzle: n.id, reading: nums[n.id] })) });
    setActive(a); setNums({}); setError(null);
  };

  const rows = active ? nozzles.filter((n) => n.id in active.starts) : [];
  const handed = [
    ...TYPES.map(([t]) => ({ amount: Number(cash[t] || 0), rate: 1 })),
    { amount: Number(other.amount || 0), rate: Number(other.rate || 0) },
  ];
  const calc = active && station ? expectedVsHanded(
    rows.filter((n) => nums[n.id]).map((n) => ({ fuel_type: n.pump.fuel_type, start: active.starts[n.id], end: Number(nums[n.id]), meter_max: Number(n.meter_max) })),
    station.prices, station.pricing_currency, new Date(active.started_at), handed) : null;

  const end = async () => {
    if (!active) return;
    const missing = rows.filter((n) => !nums[n.id]);
    if (missing.length) return setError(`Enter the end number for ${missing.length} more pump${missing.length > 1 ? "s" : ""}.`);
    const cur = station!.pricing_currency;
    const lines = TYPES.filter(([t]) => cash[t]).map(([t]) => ({ payment_type: t, currency: cur, amount: cash[t] }));
    if (other.amount) lines.push({ payment_type: "cash", currency: other.currency.toUpperCase(), amount: other.amount });
    await enqueue("shift_end", { shift_id: active.id, ended_at: new Date().toISOString(), readings: rows.map((n) => ({ nozzle: n.id, reading: nums[n.id] })), cash: lines });
    await kvSet("activeShift", null);
    setActive(null); setSaved(true); setNums({}); setCash({}); setError(null);
  };

  if (saved) return (
    <section className="card"><h1>Shift saved</h1><p>It is safe on this phone and will sync when there is signal.</p>
      <button className="btn primary" onClick={() => { setSaved(false); navigate(0); }}>Done</button></section>
  );
  if (!station) return <p className="muted center">No stations yet. Ask the owner to set up a station.</p>;

  return (
    <>
      <div className="row between"><h1>{active ? "End shift" : "Start shift"}</h1>{active && <Badge kind="active">open</Badge>}</div>
      {!active && stations.length > 1 && (
        <label>Station<select value={stationId} onChange={(e) => setStationId(e.target.value)}>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      )}
      {(active ? rows : nozzles).map((n) => (
        <label key={n.id} className={`card pump ${highlight === n.pump.id ? "hl" : ""}`}>
          <strong>{n.pump.name} · {n.label}</strong><span className="muted">{n.pump.fuel_code}{active ? ` · started at ${active.starts[n.id]}` : n.last_reading ? ` · last ${n.last_reading}` : ""}</span>
          <input inputMode="decimal" value={nums[n.id] ?? ""} placeholder={active ? "Meter number now" : "Meter number"} onChange={(e) => setNums({ ...nums, [n.id]: e.target.value.replace(",", ".") })} />
        </label>
      ))}
      {active && (
        <section className="card form">
          <h2>Handed in ({station.pricing_currency})</h2>
          {TYPES.map(([t, label]) => (
            <label key={t}>{label}<input inputMode="decimal" value={cash[t] ?? ""} onChange={(e) => setCash({ ...cash, [t]: e.target.value })} /></label>
          ))}
          <div className="row gap"><input aria-label="Other currency" style={{ width: 80 }} value={other.currency} onChange={(e) => setOther({ ...other, currency: e.target.value })} />
            <input aria-label="Other amount" inputMode="decimal" placeholder="Amount in other currency" value={other.amount} onChange={(e) => setOther({ ...other, amount: e.target.value })} />
            <input aria-label="Rate" inputMode="decimal" style={{ width: 90 }} placeholder={`rate→${station.pricing_currency}`} value={other.rate} onChange={(e) => setOther({ ...other, rate: e.target.value })} /></div>
          {calc && (
            <div className={`summary ${Math.abs(calc.variance) < 1 ? "ok" : "off"}`} aria-live="polite">
              <div>Expected <strong>{money(calc.expected)}</strong></div><div>Handed in <strong>{money(calc.submitted)}</strong></div>
              <div>Difference <strong>{money(calc.variance)}</strong></div>
            </div>
          )}
        </section>
      )}
      <ErrorNote message={error} />
      <button className="btn primary big" onClick={() => void (active ? end() : start())}>{active ? "End shift" : "Start shift"}</button>
    </>
  );
}
