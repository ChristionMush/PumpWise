import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorNote } from "../components";
import { enqueue, expectedVsHanded, kvGet, kvSet, uuid, type PriceRow } from "../offline";

export interface Nozzle { id: string; number: number; label: string; meter_max: string; last_reading: string | null }
export interface PumpInfo { id: string; name: string; qr_token: string; fuel_type: string; fuel_code: string; nozzles: Nozzle[] }
export interface StationInfo {
  id: string; name: string; pricing_currency: string; pumps: PumpInfo[]; prices: PriceRow[]; open_shift_id: string | null;
  tanks?: any[]; // only sent to managers and owners (attendants never see tank data)
  attendants_mode?: "single" | "multiple"; assignment_mode?: "shared" | "assigned";
  joinable_shift?: { id: string; started_at: string; people: string[] } | null;
  staff?: { id: string; name: string }[];
  previous_assignments?: Record<string, string>;
}
/** The shift this phone is working. `member`: joined someone else's shift (hands in cash only; the lead enters meters). */
export interface Active { id: string; station_id: string; started_at: string; starts: Record<string, number>; member?: boolean }

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
  const { user } = useAuth();
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [stationId, setStationId] = useState("");
  const [active, setActive] = useState<Active | null>(null);
  const [nums, setNums] = useState<Record<string, string>>({});
  const [cash, setCash] = useState<Record<string, string>>({});
  const [other, setOther] = useState({ currency: "ZWG", amount: "", rate: "" });
  const [assign, setAssign] = useState<Record<string, string>>({});
  const [swap, setSwap] = useState<{ pump: string; to: string; nums: Record<string, string> }>({ pump: "", to: "", nums: {} });
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
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
  const assigned = station?.assignment_mode === "assigned";
  const staff = station?.staff ?? [];

  // Assigned pumps: pre-fill from the last shift so starting is one tap; anyone missing defaults to me.
  useEffect(() => {
    if (!station || !assigned) return;
    setAssign(Object.fromEntries(station.pumps.map((p) => [p.id, station.previous_assignments?.[p.id] ?? user?.id ?? ""])));
  }, [station, assigned, user?.id]);

  const start = async () => {
    if (!station) return;
    const readings = nozzles.filter((n) => nums[n.id] !== "" && nums[n.id] !== undefined);
    if (!readings.length) return setError("Enter the meter number for at least one pump.");
    if (assigned && station.pumps.some((p) => !assign[p.id])) return setError("Choose who works each pump.");
    const a: Active = { id: uuid(), station_id: station.id, started_at: new Date().toISOString(), starts: Object.fromEntries(readings.map((n) => [n.id, Number(nums[n.id])])) };
    await kvSet("activeShift", a); // saved on the phone first
    const others = assigned ? [...new Set(Object.values(assign))].filter((id) => id && id !== user?.id) : [];
    await enqueue("shift_start", {
      shift_id: a.id, station: station.id, started_at: a.started_at, readings: readings.map((n) => ({ nozzle: n.id, reading: nums[n.id] })),
      ...(assigned ? { assignments: assign, attendants: others } : {}),
    });
    setActive(a); setNums({}); setError(null);
  };

  const join = async () => {
    const open = station?.joinable_shift;
    if (!station || !open) return;
    const a: Active = { id: open.id, station_id: station.id, started_at: open.started_at, starts: {}, member: true };
    await kvSet("activeShift", a);
    await enqueue("shift_join", { shift_id: open.id, at: new Date().toISOString() });
    setActive(a); setError(null);
  };

  const cashLines = () => {
    const cur = station!.pricing_currency;
    const lines = TYPES.filter(([t]) => cash[t]).map(([t]) => ({ payment_type: t, currency: cur, amount: cash[t] }));
    if (other.amount) lines.push({ payment_type: "cash", currency: other.currency.toUpperCase(), amount: other.amount });
    return lines;
  };

  const finish = async (message: string) => {
    await kvSet("activeShift", null);
    setActive(null); setSaved(message); setNums({}); setCash({}); setError(null);
  };

  const rows = active ? nozzles.filter((n) => n.id in active.starts) : [];
  const handed = [
    ...TYPES.map(([t]) => ({ amount: Number(cash[t] || 0), rate: 1 })),
    { amount: Number(other.amount || 0), rate: Number(other.rate || 0) },
  ];
  const calc = active && station && !active.member ? expectedVsHanded(
    rows.filter((n) => nums[n.id]).map((n) => ({ fuel_type: n.pump.fuel_type, start: active.starts[n.id], end: Number(nums[n.id]), meter_max: Number(n.meter_max) })),
    station.prices, station.pricing_currency, new Date(active.started_at), handed) : null;

  const end = async () => {
    if (!active) return;
    if (active.member) { // someone else leads: I only hand in my cash
      await enqueue("shift_cash", { shift_id: active.id, cash: cashLines(), at: new Date().toISOString() });
      return finish("Your cash is saved. The lead closes the shift.");
    }
    const missing = rows.filter((n) => !nums[n.id]);
    if (missing.length) return setError(`Enter the end number for ${missing.length} more pump${missing.length > 1 ? "s" : ""}.`);
    await enqueue("shift_end", { shift_id: active.id, ended_at: new Date().toISOString(), readings: rows.map((n) => ({ nozzle: n.id, reading: nums[n.id] })), cash: cashLines() });
    return finish(station?.attendants_mode === "multiple"
      ? "Saved. The shift closes once everyone on it has handed in."
      : "It is safe on this phone and will sync when there is signal.");
  };

  // Assigned pumps: hand a pump to someone else mid-shift (break, swap). The meter numbers at the swap split the litres.
  const swapPump = async () => {
    if (!active || !station) return;
    const pump = station.pumps.find((p) => p.id === swap.pump);
    if (!pump || !swap.to) return setError("Choose the pump and who takes it over.");
    if (pump.nozzles.some((n) => !swap.nums[n.id])) return setError("Enter the meter number of each nozzle on that pump now.");
    await enqueue("pump_reassign", { shift_id: active.id, pump: pump.id, user: swap.to, at: new Date().toISOString(), readings: pump.nozzles.map((n) => ({ nozzle: n.id, reading: swap.nums[n.id] })) });
    setSwap({ pump: "", to: "", nums: {} }); setError(null);
    setNote(`${pump.name} handed to ${staff.find((s) => s.id === swap.to)?.name ?? "the next attendant"}.`);
  };

  const leave = async () => {
    if (!active) return;
    await enqueue("shift_leave", { shift_id: active.id, at: new Date().toISOString() });
    return finish("You have left the shift.");
  };

  if (saved) return (
    <section className="card"><h1>Shift saved</h1><p>{saved}</p>
      <button className="btn primary" onClick={() => { setSaved(null); navigate(0); }}>Done</button></section>
  );
  if (!station) return <p className="muted center">No stations yet. Ask the owner to set up a station.</p>;

  const title = active ? (active.member ? "Hand in my cash" : "End shift") : "Start shift";
  return (
    <>
      <div className="row between"><h1>{title}</h1>{active && <Badge kind="active">open</Badge>}</div>
      {!active && stations.length > 1 && (
        <label>Station<select value={stationId} onChange={(e) => setStationId(e.target.value)}>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      )}
      {!active && station.joinable_shift && (
        <section className="card">
          <p>A shift is already open here{station.joinable_shift.people.length ? ` (${station.joinable_shift.people.join(", ")})` : ""}.</p>
          <button className="btn primary big" onClick={() => void join()}>Join this shift</button>
        </section>
      )}
      {!active?.member && (active ? rows : nozzles).map((n) => (
        <label key={n.id} className={`card pump ${highlight === n.pump.id ? "hl" : ""}`}>
          <strong>{n.pump.name} · {n.label}</strong><span className="muted">{n.pump.fuel_code}{active ? ` · started at ${active.starts[n.id]}` : n.last_reading ? ` · last ${n.last_reading}` : ""}</span>
          <input inputMode="decimal" value={nums[n.id] ?? ""} placeholder={active ? "Meter number now" : "Meter number"} onChange={(e) => setNums({ ...nums, [n.id]: e.target.value.replace(",", ".") })} />
        </label>
      ))}
      {!active && assigned && (
        <section className="card form">
          <h2>Who works each pump</h2>
          {station.pumps.map((p) => (
            <label key={p.id}>{p.name}
              <select aria-label={`Who works ${p.name}`} value={assign[p.id] ?? ""} onChange={(e) => setAssign({ ...assign, [p.id]: e.target.value })}>
                <option value="">Choose…</option>
                {staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </label>
          ))}
        </section>
      )}
      {active && (
        <section className="card form">
          <h2>{active.member ? "My cash" : "Handed in"} ({station.pricing_currency})</h2>
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
      {active && !active.member && assigned && (
        <section className="card form">
          <h2>Hand a pump to someone else</h2>
          <label>Pump<select value={swap.pump} onChange={(e) => setSwap({ ...swap, pump: e.target.value, nums: {} })}>
            <option value="">Choose…</option>{station.pumps.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
          <label>To<select value={swap.to} onChange={(e) => setSwap({ ...swap, to: e.target.value })}>
            <option value="">Choose…</option>{staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
          {station.pumps.find((p) => p.id === swap.pump)?.nozzles.map((n) => (
            <label key={n.id}>{n.label}: meter number now<input inputMode="decimal" value={swap.nums[n.id] ?? ""} onChange={(e) => setSwap({ ...swap, nums: { ...swap.nums, [n.id]: e.target.value.replace(",", ".") } })} /></label>
          ))}
          <button className="btn" onClick={() => void swapPump()}>Swap pump</button>
        </section>
      )}
      {note && <p role="status" className="ok-note">{note}</p>}
      <ErrorNote message={error} />
      <button className="btn primary big" onClick={() => void (active ? end() : start())}>{active ? (active.member ? "Hand in my cash" : "End shift") : "Start shift"}</button>
      {active && station.attendants_mode === "multiple" && <button className="btn" onClick={() => void leave()}>Leave the shift</button>}
    </>
  );
}
