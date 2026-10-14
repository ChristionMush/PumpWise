import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import { loadPrefill, type PumpInfo, type StationInfo } from "./Shift";

/**
 * Managers and owners: who works which pump (assigned-pump stations). Attendants never assign or swap pumps.
 * For a pump in use there are two ways to give it to someone else:
 *  - hand over: the person on it keeps it until they end their part of the shift; the new person then starts on it;
 *  - swap now: the meter numbers taken now end it for one person and start it for the other, on the same shift.
 */
export default function Pumps() {
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [swapNow, setSwapNow] = useState<Record<string, boolean>>({});
  const [meters, setMeters] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => setStations(await loadPrefill());
  useEffect(() => { void load(); }, []);

  const save = async (station: StationInfo, pump: PumpInfo, now: boolean) => {
    const to = choice[pump.id] ?? pump.assigned_to ?? "";
    if (now && pump.nozzles.some((n) => !meters[n.id])) return setError(`To swap ${pump.name} now, enter the meter number of every nozzle on it.`);
    setBusy(pump.id); setError(null); setNote(null);
    try {
      await api(`/api/pumps/${pump.id}/assign/`, {
        method: "POST",
        body: { user: to || null, ...(now ? { at: new Date().toISOString(), readings: pump.nozzles.map((n) => ({ nozzle: n.id, reading: meters[n.id] })) } : {}) },
      });
      const name = station.staff?.find((s) => s.id === to)?.name ?? "nobody";
      setNote(now ? `${pump.name} swapped to ${name}.`
        : pump.in_use_by ? `${pump.name} goes to ${name} when ${pump.in_use_by} ends their part of the shift.` : `${pump.name} is now ${name === "nobody" ? "unassigned" : `${name}'s`}.`);
      setMeters({}); setChoice({}); setSwapNow({});
      await load();
    } catch (err) {
      setError(errorMessage(err));
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
            return (
              <div key={pump.id} className="pump-row">
                <label>{pump.name} <span className="muted">({pump.nozzles.map((n) => n.fuel_code).join(" + ")}){pump.in_use_by ? ` · in use by ${pump.in_use_by}` : ""}</span>
                  <select aria-label={`Who works ${pump.name}`} value={to} onChange={(e) => setChoice({ ...choice, [pump.id]: e.target.value })}>
                    <option value="">Nobody</option>
                    {(station.staff ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                </label>
                {pump.in_use_by && (changed || to !== "") && (
                  <label className="row gap"><input type="checkbox" checked={now} onChange={(e) => setSwapNow({ ...swapNow, [pump.id]: e.target.checked })} />
                    Swap now (instead of when {pump.in_use_by} ends their part)</label>
                )}
                {now && pump.nozzles.map((n) => (
                  <label key={n.id}>{n.label} ({n.fuel_code}): meter number now
                    <input inputMode="decimal" value={meters[n.id] ?? ""} onChange={(e) => setMeters({ ...meters, [n.id]: e.target.value.replace(",", ".") })} />
                  </label>
                ))}
                {(changed || now) && (
                  <button className="btn" disabled={busy === pump.id} onClick={() => void save(station, pump, now)}>
                    {now ? "Swap pump now" : pump.in_use_by ? "Hand over when they end" : "Save"}
                  </button>
                )}
              </div>
            );
          })}
        </section>
      ))}
      {note && <p role="status" className="ok-note">{note}</p>}
      <ErrorNote message={error} />
    </>
  );
}
