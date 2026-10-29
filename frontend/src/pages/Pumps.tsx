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

  const load = async () => {
    setStations(await loadPrefill());
    const gaps = await api<{ pump_gaps?: { pump: string; message: string }[] }>("/api/dashboard/").then((d) => d.pump_gaps ?? []).catch(() => []);
    setToAllocate(Object.fromEntries(gaps.map((g) => [g.pump, g.message])));
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
        </section>
      ))}
      {note && <p role="status" className="ok-note">{note}</p>}
    </>
  );
}
