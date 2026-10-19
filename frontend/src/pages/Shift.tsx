import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorNote } from "../components";
import { allOps, enqueue, expectedVsHanded, kvGet, kvSet, uuid, type PriceRow } from "../offline";

/** Each nozzle draws from its own tank, so the fuel (and its price) is per nozzle. */
export interface Nozzle { id: string; number: number; label: string; meter_max: string; last_reading: string | null; fuel_type: string; fuel_code: string }
export interface PumpInfo {
  id: string; name: string; nozzles: Nozzle[];
  assigned_to?: string | null; assigned_to_name?: string | null; // assigned-pump stations
  in_use_by?: string | null; // managers only: who has it on an open shift right now
}
export interface StationInfo {
  id: string; name: string; pricing_currency: string; pumps: PumpInfo[]; prices: PriceRow[]; open_shift_id: string | null;
  tanks?: any[]; // only sent to managers and owners (attendants never see tank data)
  attendants_mode?: "single" | "multiple"; assignment_mode?: "shared" | "assigned";
  joinable_shift?: { id: string; started_at: string; people: string[] } | null;
  staff?: { id: string; name: string }[];
  my_stage?: Stage; // where this person is in the shift cycle, from the server
  busy_shift?: { people: string[] } | null; // one-attendant station: someone else's shift is still being worked
}
/** start: no shift yet · on_shift: started, the end screen is next · done: their part of an open shift is over. */
export type Stage =
  | { stage: "start" }
  | { stage: "on_shift"; shift: { id: string; started_at: string; member: boolean; starts: Record<string, string> } }
  | { stage: "done"; shift_id: string; ended_at: string };
/** The shift this phone is working. `member`: joined someone else's shift (hands in cash only; the lead enters meters). */
export interface Active { id: string; station_id: string; started_at: string; starts: Record<string, number>; member?: boolean }

/** Loads the phone's working data; falls back to the saved copy when there is no signal. */
export async function loadStations(): Promise<{ stations: StationInfo[]; error: string | null }> {
  try {
    const res = await api<{ stations: StationInfo[] }>("/api/shifts/prefill/");
    await kvSet("prefill", res.stations);
    await kvSet("prefillFromCache", false);
    return { stations: res.stations, error: null };
  } catch (e) {
    const saved = await kvGet<StationInfo[]>("prefill");
    await kvSet("prefillFromCache", true);
    return { stations: saved ?? [], error: saved ? null : errorMessage(e) };
  }
}

export async function loadPrefill(): Promise<StationInfo[]> {
  return (await loadStations()).stations;
}

/** The phone keeps one person's working copy. When someone else signs in on it, the previous person's saved
 *  stations and open shift are dropped (their queued entries still sync). */
async function forgetOtherUser(userId: string) {
  const owner = await kvGet<string>("phoneUser");
  if (owner && owner !== userId) {
    await kvSet("prefill", null);
    await kvSet("activeShift", null);
  }
  await kvSet("phoneUser", userId);
}

/** The screen this phone should show. The server knows where the person is (even after signing out and in, or on
 *  another phone); only entries this phone has not sent yet can be newer than what the server knows. */
async function settle(local: Active | undefined, st: StationInfo[], fresh: boolean): Promise<{ active: Active | null; waiting: boolean }> {
  const pending = await allOps();
  if (local && !st.some((s) => s.id === local!.station_id)) local = undefined; // a station this person no longer has
  if (local) {
    const unsent = pending.some((o) => o.payload?.shift_id === local!.id);
    const server = st.find((s) => s.id === local!.station_id)?.my_stage;
    if (unsent || !fresh || !server) return { active: local, waiting: false };
    if (server.stage === "on_shift") { // the server's shift and its numbers win (this phone's may be merged or stale)
      const starts = { ...local.starts, ...Object.fromEntries(Object.entries(server.shift.starts).map(([k, v]) => [k, Number(v)])) };
      return { active: { ...local, id: server.shift.id, started_at: server.shift.started_at, starts, member: server.shift.member }, waiting: false };
    }
    return { active: null, waiting: false }; // ended or closed elsewhere (another phone, a manager)
  }
  const ended = await kvGet<{ station_id: string; at: string }>("lastEnded");
  for (const s of st) {
    const server = s.my_stage;
    if (server?.stage !== "on_shift") continue;
    // this phone already ended it and the end has not been sent yet: do not bring the shift back
    if (ended?.station_id === s.id && ended.at >= server.shift.started_at && pending.length) return { active: null, waiting: true };
    const starts = Object.fromEntries(Object.entries(server.shift.starts).map(([k, v]) => [k, Number(v)]));
    return { active: { id: server.shift.id, station_id: s.id, started_at: server.shift.started_at, starts, member: server.shift.member }, waiting: false };
  }
  return { active: null, waiting: false };
}

/** Start shift, then on shift, then ended: where this person is, at a glance. */
function Stages({ current }: { current: 0 | 1 | 2 }) {
  const names = ["Start shift", "On shift: end it when you finish", "Shift ended"];
  return (
    <ol className="stages" aria-label="Shift stage">
      {names.map((n, i) => <li key={n} className={i === current ? "now" : i < current ? "past" : ""} aria-current={i === current ? "step" : undefined}>{n}</li>)}
    </ol>
  );
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
  const [saved, setSaved] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const { user } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!user) return;
    void (async () => {
      await forgetOtherUser(user.id);
      const { stations: st, error: problem } = await loadStations();
      setStations(st); setLoadError(problem); setLoaded(true);
      const fresh = !problem && !(await kvGet<boolean>("prefillFromCache"));
      const settled = await settle((await kvGet<Active>("activeShift")) ?? undefined, st, fresh);
      const a = settled.active;
      await kvSet("activeShift", a);
      setActive(a); setWaiting(settled.waiting);
      setStationId(a?.station_id ?? st[0]?.id ?? "");
      // prefill with the last numbers; never for a pump still with someone else (entering its numbers takes it over)
      if (!a && st[0]) setNums(Object.fromEntries(st[0].pumps.filter((p) => !p.in_use_by).flatMap((p) => p.nozzles.map((n) => [n.id, n.last_reading ?? ""]))));
    })();
  }, [user]);

  // While on shift, keep in step with the server: a pump handed to me mid-shift (the outgoing attendant ended their
  // part, or a manager swapped it) appears on my end screen. Checked when the phone comes back to the app, and every minute.
  useEffect(() => {
    if (!active) return;
    const check = async () => {
      if (document.visibilityState === "hidden") return;
      const { stations: st, error: problem } = await loadStations();
      if (problem) return;
      const settled = await settle((await kvGet<Active>("activeShift")) ?? undefined, st, !(await kvGet<boolean>("prefillFromCache")));
      setStations(st);
      if (settled.active && JSON.stringify(settled.active) !== JSON.stringify(active)) {
        await kvSet("activeShift", settled.active);
        setActive(settled.active);
      }
    };
    const timer = setInterval(() => void check(), 60000);
    document.addEventListener("visibilitychange", check);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", check); };
  }, [active]);

  const station = stations.find((s) => s.id === stationId);
  // On assigned pumps the server sends an attendant only the pumps a manager gave them.
  const nozzles = useMemo(() => station?.pumps.flatMap((p) => p.nozzles.map((n) => ({ ...n, pump: p }))) ?? [], [station]);
  const assigned = station?.assignment_mode === "assigned";

  const start = async () => {
    if (!station) return;
    const readings = nozzles.filter((n) => nums[n.id] !== "" && nums[n.id] !== undefined);
    if (!readings.length) return setError("Enter the meter number for at least one pump.");
    // Assigned pumps: every nozzle of my free pumps. A pump still with the outgoing attendant is taken over only if
    // I enter its numbers (when they hand it to me).
    if (assigned && nozzles.some((n) => !n.pump.in_use_by && !readings.includes(n))) return setError("Enter the meter number of every nozzle on your pumps.");
    const a: Active = { id: uuid(), station_id: station.id, started_at: new Date().toISOString(), starts: Object.fromEntries(readings.map((n) => [n.id, Number(nums[n.id])])) };
    await kvSet("activeShift", a); // saved on the phone first
    await enqueue("shift_start", { shift_id: a.id, station: station.id, started_at: a.started_at, readings: readings.map((n) => ({ nozzle: n.id, reading: nums[n.id] })) });
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
    if (active) await kvSet("lastEnded", { station_id: active.station_id, at: new Date().toISOString() });
    await kvSet("activeShift", null);
    setActive(null); setSaved(message); setNums({}); setCash({}); setError(null);
  };

  // A pump the manager swapped away mid-shift drops out of the prefill: its end number was taken at the swap.
  const rows = active ? nozzles.filter((n) => n.id in active.starts) : [];
  const handed = [
    ...TYPES.map(([t]) => ({ amount: Number(cash[t] || 0), rate: 1 })),
    { amount: Number(other.amount || 0), rate: Number(other.rate || 0) },
  ];
  const calc = active && station && !active.member ? expectedVsHanded(
    rows.filter((n) => nums[n.id]).map((n) => ({ fuel_type: n.fuel_type, start: active.starts[n.id], end: Number(nums[n.id]), meter_max: Number(n.meter_max) })),
    station.prices, station.pricing_currency, new Date(active.started_at), handed) : null;

  const end = async () => {
    if (!active) return;
    if (active.member) { // someone else leads: I only hand in my cash
      await enqueue("shift_cash", { shift_id: active.id, cash: cashLines(), at: new Date().toISOString() });
      return finish("Your cash is saved. The lead closes the shift.");
    }
    const missing = rows.filter((n) => !nums[n.id]);
    if (missing.length) return setError(`Enter the end number for ${missing.length} more nozzle${missing.length > 1 ? "s" : ""}.`);
    await enqueue("shift_end", { shift_id: active.id, ended_at: new Date().toISOString(), readings: rows.map((n) => ({ nozzle: n.id, reading: nums[n.id] })), cash: cashLines() });
    return finish(assigned
      ? "Your part of the shift is saved. Your pumps pass to the next attendant."
      : station?.attendants_mode === "multiple"
        ? "Saved. The shift closes once everyone on it has handed in."
        : "It is safe on this phone and will sync when there is signal.");
  };

  const leave = async () => {
    if (!active) return;
    await enqueue("shift_leave", { shift_id: active.id, at: new Date().toISOString() });
    return finish("You have left the shift.");
  };

  if (saved) return (
    <section className="card"><Stages current={2} /><h1>Shift saved</h1><p>{saved}</p>
      <button className="btn primary" onClick={() => { setSaved(null); navigate(0); }}>Done</button></section>
  );
  if (!station) {
    if (!loaded) return <p className="muted center">Loading…</p>;
    if (loadError) return (
      <section className="card" role="alert"><p>Could not load your stations: {loadError}</p>
        <button className="btn" onClick={() => navigate(0)}>Try again</button></section>
    );
    return <p className="muted center">No station is set up for you yet. Ask the owner to add your station (and on assigned pumps, your manager to give you your pumps).</p>;
  }

  const stage = station.my_stage;
  if (!active && waiting) return (
    <section className="card"><Stages current={2} /><h1>Shift ended</h1>
      <p>Your end of shift is saved on this phone and will be sent when there is signal.</p></section>
  );
  if (!active && stage?.stage === "done") return (
    <section className="card"><Stages current={2} /><h1>Your part of the shift is done</h1>
      <p>You ended your part at {new Date(stage.ended_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. Your pumps have passed to the next attendant.</p>
      <p className="muted">You can start again when the next shift opens.</p></section>
  );

  const title = active ? (active.member ? "Hand in my cash" : assigned ? "End my part of the shift" : "End shift") : "Start shift";
  const noPumps = !active && assigned && station.pumps.length === 0;
  const busy = !active && station.busy_shift ? station.busy_shift.people : null;
  return (
    <>
      <Stages current={active ? 1 : 0} />
      <div className="row between"><h1>{title}</h1>{active && <Badge kind="active">open</Badge>}</div>
      {active && <p className="muted">Started {new Date(active.started_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}. Enter the numbers now and what you hand in, then end the shift.</p>}
      {!active && stations.length > 1 && (
        <label>Station<select value={stationId} onChange={(e) => setStationId(e.target.value)}>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      )}
      {!active && assigned && station.pumps.length > 0 && <p className="muted">Your pumps: {station.pumps.map((p) => p.name).join(", ")}</p>}
      {!active && assigned && station.pumps.filter((p) => p.in_use_by).map((p) => (
        <p key={p.id} className="warn" role="status">{p.name} is still with {p.in_use_by}. Enter its numbers only when they hand it over to you.</p>
      ))}
      {busy && <section className="card" role="status"><p>A shift is open here ({busy.join(", ")}). It must be ended, or handed over to you, before you can start.</p></section>}
      {noPumps && <section className="card" role="status"><p>No pump is assigned to you at {station.name}. Ask your manager to assign your pumps.</p></section>}
      {!active && station.joinable_shift && (
        <section className="card">
          <p>A shift is already open here{station.joinable_shift.people.length ? ` (${station.joinable_shift.people.join(", ")})` : ""}.</p>
          <button className="btn primary big" onClick={() => void join()}>Join this shift</button>
        </section>
      )}
      {!active?.member && (active ? rows : nozzles).map((n) => (
        <label key={n.id} className="card pump">
          <strong>{n.pump.name} · {n.label}</strong><span className="muted">{n.fuel_code}{active ? ` · started at ${active.starts[n.id]}` : n.last_reading ? ` · last ${n.last_reading}` : ""}</span>
          <input inputMode="decimal" value={nums[n.id] ?? ""} placeholder={active ? "Meter number now" : "Meter number"} onChange={(e) => setNums({ ...nums, [n.id]: e.target.value.replace(",", ".") })} />
        </label>
      ))}
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
      <ErrorNote message={error} />
      {!noPumps && !busy && <button className="btn primary big" onClick={() => void (active ? end() : start())}>{active ? (active.member ? "Hand in my cash" : "End shift") : "Start shift"}</button>}
      {active && assigned && <p className="muted">Ending hands your pumps to the next attendant. The shift goes on for the others.</p>}
      {active && station.attendants_mode === "multiple" && !assigned && <button className="btn" onClick={() => void leave()}>Leave the shift</button>}
    </>
  );
}
