import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import { loadPrefill, type PumpInfo, type StationInfo } from "./Shift";

/**
 * Managers and owners: who works which pump (assigned-pump stations). Attendants never assign or swap pumps.
 * Changing a pump that is in use on someone's open shift is a swap: the meter numbers taken now end it on that
 * person's shift and start it on the next person's, so each answers for their own litres and cash.
 */
export default function Pumps() {
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [meters, setMeters] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => setStations(await loadPrefill());
  useEffect(() => { void load(); }, []);

  const save = async (station: StationInfo, pump: PumpInfo) => {
    const to = choice[pump.id] ?? pump.assigned_to ?? "";
    const swap = Boolean(pump.in_use_by) && to !== (pump.assigned_to ?? "");
    if (swap && pump.nozzles.some((n) => !meters[n.id])) return setError(`${pump.name} is in use. Enter the meter number of both nozzles now.`);
    setBusy(pump.id); setError(null); setNote(null);
    try {
      await api(`/api/pumps/${pump.id}/assign/`, {
        method: "POST",
        body: { user: to || null, ...(swap ? { at: new Date().toISOString(), readings: pump.nozzles.map((n) => ({ nozzle: n.id, reading: meters[n.id] })) } : {}) },
      });
      const name = station.staff?.find((s) => s.id === to)?.name;
      setNote(swap ? `${pump.name} swapped to ${name ?? "nobody"}.` : `${pump.name} is now ${name ? `${name}'s` : "unassigned"}.`);
      setMeters({}); setChoice({});
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
            const swap = Boolean(pump.in_use_by) && to !== (pump.assigned_to ?? "");
            return (
              <div key={pump.id} className="pump-row">
                <label>{pump.name} <span className="muted">({pump.nozzles.map((n) => n.fuel_code).join(" + ")}){pump.in_use_by ? ` · in use by ${pump.in_use_by}` : ""}</span>
                  <select aria-label={`Who works ${pump.name}`} value={to} onChange={(e) => setChoice({ ...choice, [pump.id]: e.target.value })}>
                    <option value="">Nobody</option>
                    {(station.staff ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                  </select>
                </label>
                {swap && pump.nozzles.map((n) => (
                  <label key={n.id}>{n.label} ({n.fuel_code}): meter number now
                    <input inputMode="decimal" value={meters[n.id] ?? ""} onChange={(e) => setMeters({ ...meters, [n.id]: e.target.value.replace(",", ".") })} />
                  </label>
                ))}
                {to !== (pump.assigned_to ?? "") && (
                  <button className="btn" disabled={busy === pump.id} onClick={() => void save(station, pump)}>{swap ? "Swap pump" : "Save"}</button>
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
