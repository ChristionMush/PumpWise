import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ErrorNote } from "../components";
import { enqueue, uuid } from "../offline";
import { loadPrefill, type StationInfo } from "./Shift";

export default function Dip() {
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [tankId, setTankId] = useState("");
  const [litres, setLitres] = useState("");
  const [dipMm, setDipMm] = useState("");
  const [delivered, setDelivered] = useState({ litres: "", supplier: "", note: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { void loadPrefill().then((s) => { setStations(s); setTankId(s[0]?.tanks?.[0]?.id ?? ""); }); }, []);
  const tanks = stations.flatMap((s) => (s.tanks ?? []).map((t) => ({ ...t, station: s.name })));
  const tank = tanks.find((t) => t.id === tankId);

  const saveDip = async (e: FormEvent) => {
    e.preventDefault();
    if (!litres && !dipMm) return setError("Enter the dip height or the litres.");
    if (dipMm && !litres && !tank?.has_calibration) return setError("This tank has no calibration chart yet. Enter litres instead.");
    // client_id: made on the phone, so a retry can never record the same dip twice (rule 8)
    await enqueue("tank_dip", { client_id: uuid(), tank: tankId, at: new Date().toISOString(), ...(litres ? { litres } : {}), ...(dipMm ? { dip_mm: dipMm } : {}) });
    setLitres(""); setDipMm(""); setError(null); setMsg("Dip saved.");
  };
  const saveDelivery = async (e: FormEvent) => {
    e.preventDefault();
    if (!delivered.litres) return setError("Enter the litres received.");
    await enqueue("delivery", { client_id: uuid(), tank: tankId, litres: delivered.litres, delivered_at: new Date().toISOString(), supplier: delivered.supplier, note_number: delivered.note });
    setDelivered({ litres: "", supplier: "", note: "" }); setError(null); setMsg("Delivery saved.");
  };

  return (
    <>
      <div className="row between"><h1>Tank dip and deliveries</h1><Link to="/dip-history">See dip history</Link></div>
      <label>Tank<select value={tankId} onChange={(e) => setTankId(e.target.value)}>{tanks.map((t) => <option key={t.id} value={t.id}>{t.station} · {t.name} ({t.fuel_code})</option>)}</select></label>
      <form onSubmit={saveDip} className="card form"><h2>Dip reading</h2>
        <label>Dip height (mm)<input inputMode="decimal" value={dipMm} onChange={(e) => setDipMm(e.target.value)} /></label>
        <label>or litres<input inputMode="decimal" value={litres} onChange={(e) => setLitres(e.target.value)} /></label>
        <button className="btn primary">Save dip</button></form>
      <form onSubmit={saveDelivery} className="card form"><h2>Fuel delivery received</h2>
        <label>Litres received<input inputMode="decimal" value={delivered.litres} onChange={(e) => setDelivered({ ...delivered, litres: e.target.value })} /></label>
        <label>Supplier<input value={delivered.supplier} onChange={(e) => setDelivered({ ...delivered, supplier: e.target.value })} /></label>
        <label>Delivery note no.<input value={delivered.note} onChange={(e) => setDelivered({ ...delivered, note: e.target.value })} /></label>
        <button className="btn">Save delivery</button></form>
      <ErrorNote message={error} />{msg && <p role="status" className="ok-note">{msg}</p>}
    </>
  );
}
