import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import { enqueue, uuid } from "../offline";
import { loadPrefill, type StationInfo } from "./Shift";

interface CloseStatus { station: string; day: string; closed: { day: string; closed_at: string; closed_by: string | null } | null; recent: { id: string; day: string; closed_at: string; closed_by: string | null }[] }

const dayName = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Managers: close the trading day at about 21:00 (every tank's dip and every pump's meter in one go), record fuel
 * deliveries, and take a spot dip if needed. The day close is what the daily figures and the daily report run on. */
export default function Dip() {
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [stationId, setStationId] = useState("");
  const [status, setStatus] = useState<CloseStatus[]>([]);
  const [dips, setDips] = useState<Record<string, { mm: string; litres: string }>>({});
  const [meters, setMeters] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [tankId, setTankId] = useState("");
  const [litres, setLitres] = useState("");
  const [dipMm, setDipMm] = useState("");
  const [delivered, setDelivered] = useState({ fuel: "", tank: "", litres: "", supplier: "", note: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = () => api<CloseStatus[]>("/api/day-close/").then(setStatus).catch(() => undefined);
  useEffect(() => {
    void loadPrefill().then((s) => { setStations(s); setStationId(s[0]?.id ?? ""); setTankId(s[0]?.tanks?.[0]?.id ?? ""); });
    void loadStatus();
  }, []);
  const station = stations.find((s) => s.id === stationId);
  const stationTanks = station?.tanks ?? [];
  const nozzles = (station?.pumps ?? []).flatMap((p) => p.nozzles.map((n) => ({ ...n, pump: p })));
  const today = status.find((s) => s.station === stationId);
  const tanks = stations.flatMap((s) => (s.tanks ?? []).map((t) => ({ ...t, station: s.name })));
  const tank = tanks.find((t) => t.id === tankId);

  const closeDay = async (e: FormEvent) => {
    e.preventDefault();
    if (!station) return;
    const missingDip = stationTanks.filter((t) => !dips[t.id]?.mm && !dips[t.id]?.litres);
    const missingMeter = nozzles.filter((n) => !meters[n.id]);
    if (missingDip.length || missingMeter.length) {
      return setError(`Enter every tank's dip and every pump's meter. Missing: ${[...missingDip.map((t) => t.name), ...missingMeter.map((n) => `${n.pump.name} ${n.label}`)].join(", ")}.`);
    }
    const noChart = stationTanks.find((t) => dips[t.id]?.mm && !dips[t.id]?.litres && !t.has_calibration);
    if (noChart) return setError(`${noChart.name} has no calibration chart yet. Enter its litres instead.`);
    setBusy(true); setError(null); setMsg(null);
    try {
      await api("/api/day-close/", { method: "POST", body: {
        station: station.id, note,
        dips: stationTanks.map((t) => ({ tank: t.id, ...(dips[t.id].litres ? { litres: dips[t.id].litres } : { dip_mm: dips[t.id].mm }) })),
        meters: nozzles.map((n) => ({ nozzle: n.id, reading: meters[n.id] })),
      } });
      setDips({}); setMeters({}); setNote(""); setMsg(`${station.name}: the day is closed.`);
      await loadStatus();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const saveDip = async (e: FormEvent) => {
    e.preventDefault();
    if (!litres && !dipMm) return setError("Enter the dip height or the litres.");
    if (dipMm && !litres && !tank?.has_calibration) return setError("This tank has no calibration chart yet. Enter litres instead.");
    // client_id: made on the phone, so a retry can never record the same dip twice (rule 8)
    await enqueue("tank_dip", { client_id: uuid(), tank: tankId, at: new Date().toISOString(), ...(litres ? { litres } : {}), ...(dipMm ? { dip_mm: dipMm } : {}) });
    setLitres(""); setDipMm(""); setError(null); setMsg("Dip saved.");
  };
  const fuels = [...new Set(tanks.map((t) => t.fuel_code))];
  const fuelTanks = tanks.filter((t) => t.fuel_code === delivered.fuel);
  const deliveryTank = fuelTanks.length === 1 ? fuelTanks[0].id : delivered.tank;
  const saveDelivery = async (e: FormEvent) => {
    e.preventDefault();
    if (!delivered.fuel) return setError("Choose the fuel being received.");
    if (!deliveryTank) return setError("Choose the tank the fuel goes into.");
    if (!delivered.litres) return setError("Enter the litres received.");
    await enqueue("delivery", { client_id: uuid(), tank: deliveryTank, fuel_code: delivered.fuel, litres: delivered.litres, delivered_at: new Date().toISOString(), supplier: delivered.supplier, note_number: delivered.note });
    setDelivered({ fuel: "", tank: "", litres: "", supplier: "", note: "" }); setError(null); setMsg("Delivery saved.");
  };

  return (
    <>
      <div className="row between"><h1>Day close, dips and deliveries</h1><Link to="/dip-history">See dip history</Link></div>
      {stations.length > 1 && <label>Station<select value={stationId} onChange={(e) => setStationId(e.target.value)}>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}

      <form onSubmit={closeDay} className="card form" aria-label="Close the day">
        <h2>Close the day{today ? `: ${dayName(today.day)}` : ""}</h2>
        {today?.closed ? (
          <p className="ok-note">Closed at {time(today.closed.closed_at)}{today.closed.closed_by ? ` by ${today.closed.closed_by}` : ""}.</p>
        ) : (<>
          <p className="muted">At about 21:00, pause the pumps, dip every tank and read every pump's meter, then close the day. The day's sales, fuel check and report run from one close to the next.</p>
          <h3>Tank dips</h3>
          {stationTanks.map((t) => (
            <div key={t.id} className="row gap">
              <strong>{t.name} <span className="muted">· {t.fuel_code}</span></strong>
              {t.has_calibration && <label>Dip (mm)<input inputMode="decimal" value={dips[t.id]?.mm ?? ""} onChange={(e) => setDips({ ...dips, [t.id]: { mm: e.target.value, litres: dips[t.id]?.litres ?? "" } })} /></label>}
              <label>{t.has_calibration ? "or litres" : "Litres"}<input inputMode="decimal" value={dips[t.id]?.litres ?? ""} onChange={(e) => setDips({ ...dips, [t.id]: { mm: dips[t.id]?.mm ?? "", litres: e.target.value } })} /></label>
            </div>
          ))}
          <h3>Pump meters</h3>
          {nozzles.map((n) => (
            <label key={n.id}>{n.pump.name} · {n.label} <span className="muted">({n.fuel_code}{n.last_reading ? `, last ${n.last_reading}` : ""})</span>
              <input inputMode="decimal" value={meters[n.id] ?? ""} onChange={(e) => setMeters({ ...meters, [n.id]: e.target.value.replace(",", ".") })} />
            </label>
          ))}
          <label>Note (optional)<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
          <button className="btn primary" disabled={busy}>Close the day</button>
          <p className="muted">Needs signal. Attendants on shift keep working their pumps: their part and their cash are not affected.</p>
        </>)}
        {today && today.recent.length > 0 && (
          <details><summary className="muted">Last closes</summary>
            <ul className="plain">{today.recent.map((c) => <li key={c.id}>{dayName(c.day)}: {time(c.closed_at)}{c.closed_by ? ` · ${c.closed_by}` : ""}</li>)}</ul></details>
        )}
      </form>

      <form onSubmit={saveDelivery} className="card form"><h2>Fuel delivery received</h2>
        <label>Fuel received<select value={delivered.fuel} onChange={(e) => setDelivered({ ...delivered, fuel: e.target.value, tank: "" })}>
          <option value="">Choose the fuel</option>{fuels.map((f) => <option key={f} value={f}>{f}</option>)}</select></label>
        {fuelTanks.length > 1 && (
          <label>Into tank<select value={delivered.tank} onChange={(e) => setDelivered({ ...delivered, tank: e.target.value })}>
            <option value="">Choose the tank</option>{fuelTanks.map((t) => <option key={t.id} value={t.id}>{t.station} · {t.name}</option>)}</select></label>
        )}
        {fuelTanks.length === 1 && <p className="muted">Goes into {fuelTanks[0].station} · {fuelTanks[0].name}.</p>}
        <label>Litres received<input inputMode="decimal" value={delivered.litres} onChange={(e) => setDelivered({ ...delivered, litres: e.target.value })} /></label>
        <label>Supplier<input value={delivered.supplier} onChange={(e) => setDelivered({ ...delivered, supplier: e.target.value })} /></label>
        <label>Delivery note no.<input value={delivered.note} onChange={(e) => setDelivered({ ...delivered, note: e.target.value })} /></label>
        <button className="btn">Save delivery</button></form>

      <details className="card form"><summary><strong>Spot dip</strong> <span className="muted">· a dip outside the day close</span></summary>
        <form onSubmit={saveDip} className="form">
          <label>Tank<select value={tankId} onChange={(e) => setTankId(e.target.value)}>{tanks.map((t) => <option key={t.id} value={t.id}>{t.station} · {t.name} ({t.fuel_code})</option>)}</select></label>
          <label>Dip height (mm)<input inputMode="decimal" value={dipMm} onChange={(e) => setDipMm(e.target.value)} /></label>
          <label>or litres<input inputMode="decimal" value={litres} onChange={(e) => setLitres(e.target.value)} /></label>
          <button className="btn">Save dip</button></form>
      </details>
      <ErrorNote message={error} />{msg && <p role="status" className="ok-note">{msg}</p>}
    </>
  );
}
