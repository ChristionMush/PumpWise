import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import { loadPrefill, type PumpInfo, type StationInfo } from "./Shift";

/**
 * Managers and owners: who works which pump (assigned-pump stations). Attendants never assign or swap pumps.
 * For a pump in use there are two ways to give it to someone else:
 *  - hand over: the person on it keeps it until they end their part of the shift; the new person then starts on it;
 *  - swap now: the meter numbers taken now end it for one person and start it for the other, on the same shift.
 * Every pump keeps an attendant's name. Once someone closes their meter numbers on a pump, the manager allocates it for
 * the next shift (saving the same person again counts); until then it is listed here and at the top of Today.
 * With the weekly rotation on, each of those assignments is also remembered for its crew's day or night shift and
 * repeated by itself (see RotaCard).
 */
export default function Pumps() {
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [swapNow, setSwapNow] = useState<Record<string, boolean>>({});
  const [meters, setMeters] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<{ pump: string; message: string } | null>(null);
  const [toAllocate, setToAllocate] = useState<Record<string, string>>({}); // pump id -> why it needs allocating
  const [rotas, setRotas] = useState<Record<string, Rota>>({}); // station id -> its weekly rotation

  const load = async () => {
    setStations(await loadPrefill());
    const gaps = await api<{ pump_gaps?: { pump: string; message: string }[] }>("/api/dashboard/").then((d) => d.pump_gaps ?? []).catch(() => []);
    setToAllocate(Object.fromEntries(gaps.map((g) => [g.pump, g.message])));
    const list = await api<Rota[]>("/api/pump-rotas/").catch(() => []);
    setRotas(Object.fromEntries(list.map((r) => [r.station, r])));
  };
  useEffect(() => { void load(); }, []);

  // A free pump for someone already on shift: they start on it now, at the meter numbers entered (last numbers prefilled).
  const meterOf = (n: PumpInfo["nozzles"][number]) => meters[n.id] ?? n.last_reading ?? "";

  const save = async (station: StationInfo, pump: PumpInfo, now: boolean, startNow: boolean) => {
    const to = choice[pump.id] ?? pump.assigned_to ?? "";
    if (!to) return setError({ pump: pump.id, message: `Choose who works ${pump.name}: every pump needs an attendant's name.` });
    const withNumbers = now || startNow;
    // a swap needs the numbers read now; starting on a free pump may keep the last numbers shown
    const number = (n: PumpInfo["nozzles"][number]) => (now ? meters[n.id] ?? "" : meterOf(n));
    if (withNumbers && pump.nozzles.some((n) => !number(n))) {
      return setError({ pump: pump.id, message: `To ${now ? "swap" : "start"} ${pump.name} now, enter the meter number of every nozzle on it.` });
    }
    setBusy(pump.id); setError(null); setNote(null);
    try {
      await api(`/api/pumps/${pump.id}/assign/`, {
        method: "POST",
        body: { user: to || null, ...(withNumbers ? { at: new Date().toISOString(), readings: pump.nozzles.map((n) => ({ nozzle: n.id, reading: number(n) })) } : {}) },
      });
      const name = station.staff?.find((s) => s.id === to)?.name ?? "them";
      setNote(now ? `${pump.name} swapped to ${name}.` : startNow ? `${name} is now on ${pump.name}.`
        : pump.in_use_by ? `${pump.name} goes to ${name} when ${pump.in_use_by} ends their part of the shift.` : `${pump.name} is allocated to ${name}.`);
      setMeters({}); setChoice({}); setSwapNow({});
      await load();
    } catch (err) {
      setError({ pump: pump.id, message: errorMessage(err) });
    } finally {
      setBusy(null);
    }
  };

  const assignedStations = stations.filter((s) => s.assignment_mode === "assigned");
  return (
    <>
      <h1>Pumps</h1>
      {!assignedStations.length && <p className="muted">Pumps are shared at your stations: everyone on the shift uses every pump. Assigned pumps are set up by PumpWise.</p>}
      {assignedStations.map((station) => (
        <section key={station.id} className="card form">
          <h2>{station.name}</h2>
          {station.pumps.map((pump) => {
            const to = choice[pump.id] ?? pump.assigned_to ?? "";
            const changed = to !== (pump.assigned_to ?? "");
            const now = Boolean(swapNow[pump.id]);
            const person = (station.staff ?? []).find((s) => s.id === to);
            // free pump, chosen person on shift here and not already on it: start them on it now with its numbers
            const startNow = !pump.in_use_by && Boolean(person?.on_shift);
            return (
              <div key={pump.id} className="pump-row">
                <label>{pump.name} <span className="muted">({pump.nozzles.map((n) => n.fuel_code).join(" + ")}){pump.in_use_by ? ` · in use by ${pump.in_use_by}` : ""}</span>
                  <select aria-label={`Who works ${pump.name}`} value={to} onChange={(e) => setChoice({ ...choice, [pump.id]: e.target.value })}>
                    {!to && <option value="" disabled>Choose a name</option>}
                    {(station.staff ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                </label>
                {/* swapping only makes sense when the pump is to go to someone other than the person on it now */}
                {pump.in_use_by && (changed || pump.assigned_to_name !== pump.in_use_by) && (
                  <label className="row gap"><input type="checkbox" checked={now} onChange={(e) => setSwapNow({ ...swapNow, [pump.id]: e.target.checked })} />
                    Swap now (instead of when {pump.in_use_by} ends their part)</label>
                )}
                {toAllocate[pump.id] && <p className="warn">{toAllocate[pump.id]}</p>}
                {startNow && <p className="muted">{person?.name} is on shift now: enter {pump.name}'s meter numbers to start them on it.</p>}
                {(now || startNow) && pump.nozzles.map((n) => (
                  <label key={n.id}>{n.label} ({n.fuel_code}): meter number now
                    <input inputMode="decimal" value={now ? meters[n.id] ?? "" : meterOf(n)} onChange={(e) => setMeters({ ...meters, [n.id]: e.target.value.replace(",", ".") })} />
                  </label>
                ))}
                {(changed || now || startNow || toAllocate[pump.id]) && (
                  <button className="btn" disabled={busy === pump.id} onClick={() => void save(station, pump, now, startNow && !now)}>
                    {now ? "Swap pump now" : startNow ? `Start ${person?.name} on it now` : pump.in_use_by ? "Hand over when they end" : changed ? "Save" : `Keep ${person?.name ?? "them"} for the next shift`}
                  </button>
                )}
                {error?.pump === pump.id && <ErrorNote message={error.message} />}
              </div>
            );
          })}
          {rotas[station.id] && <RotaCard rota={rotas[station.id]} onSaved={(r) => setRotas({ ...rotas, [station.id]: r })} />}
        </section>
      ))}
      {note && <p role="status" className="ok-note">{note}</p>}
    </>
  );
}

interface RotaHolder { id: string; name: string }
interface Rota {
  station: string; station_name: string; on: boolean; week_starts: string | null;
  crew_now?: "a" | "b"; part_now?: "day" | "night";
  pumps?: { id: string; name: string; holders: Record<string, RotaHolder | null> }[];
}

const SLOTS = [["a_day", "Crew A day"], ["a_night", "Crew A night"], ["b_day", "Crew B day"], ["b_night", "Crew B night"]] as const;
const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in the phone's own time

/**
 * Two crews, one week on and one week off. With the rotation on, every pump the manager assigns is remembered for that
 * crew's day or night shift, and an hour before each later shift change the pumps switch by themselves. The table shows
 * what has been learned so far; a manual change replaces it.
 */
function RotaCard({ rota, onSaved }: { rota: Rota; onSaved: (r: Rota) => void }) {
  const [weekStarts, setWeekStarts] = useState(rota.week_starts ?? today());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (on: boolean) => {
    setBusy(true); setError(null);
    try {
      onSaved(await api<Rota>(`/api/pump-rotas/${rota.station}/`, { method: "PUT", body: { on, week_starts: weekStarts } }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pump-row">
      <h3>Weekly rotation</h3>
      {rota.on ? (
        <>
          <p className="muted">On now: Crew {rota.crew_now?.toUpperCase()}, {rota.part_now} shift. Crew A's week started {rota.week_starts}; the crews swap at 07:00 every 7 days.
            Pumps you assign are remembered for that crew's shift and switch by themselves an hour before each shift change.</p>
          <table className="table" aria-label={`Weekly rotation at ${rota.station_name}`}>
            <thead><tr><th>Pump</th>{SLOTS.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead>
            <tbody>
              {(rota.pumps ?? []).map((p) => (
                <tr key={p.id}><td>{p.name}</td>{SLOTS.map(([key]) => <td key={key}>{p.holders[key]?.name ?? <span className="muted">not yet</span>}</td>)}</tr>
              ))}
            </tbody>
          </table>
          <button className="btn" disabled={busy} onClick={() => void save(false)}>Turn off weekly rotation</button>
        </>
      ) : (
        <>
          <p className="muted">For two crews working one week on and one week off. Turn it on, then assign pumps as usual for each crew's day and night shift:
            every assignment is remembered and repeated by itself before each later shift change.</p>
          <label>This week's crew (Crew A) started on<input type="date" value={weekStarts} onChange={(e) => setWeekStarts(e.target.value)} /></label>
          <button className="btn" disabled={busy || !weekStarts} onClick={() => void save(true)}>Turn on weekly rotation</button>
        </>
      )}
      <ErrorNote message={error} />
    </div>
  );
}
