import { useEffect, useState } from "react";
import { api, downloadFile, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";

const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (n: number) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dipText = (d: { dip_mm: string | null; litres: string }) => `${d.dip_mm ? `${d.dip_mm} mm = ` : ""}${Number(d.litres).toLocaleString()} L`;
const dayName = (d: string) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const ago = (h: number) => (h < 1 ? "under an hour ago" : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`);

/** Managers and owners: when each tank was dipped, by whom, the deliveries, and the days a tank was not dipped. */
export default function DipHistory() {
  const [stations, setStations] = useState<any[]>([]);
  const [station, setStation] = useState("");
  const [from, setFrom] = useState(daysAgo(13));
  const [to, setTo] = useState(today());
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const exportSheet = async () => {
    setExporting(true); setError(null);
    const q = new URLSearchParams({ date_from: from, date_to: to, ...(station ? { station } : {}) });
    const name = stations.find((s) => s.id === station)?.name ?? "All stations";
    try { await downloadFile(`/api/reports/dip-history/export/?${q}`, `Dip history - ${name} - ${from} to ${to}.xlsx`); }
    catch (e) { setError(errorMessage(e)); }
    finally { setExporting(false); }
  };

  useEffect(() => { api<any>("/api/stations/").then((s) => setStations(s.results)).catch(() => setStations([])); }, []);
  useEffect(() => {
    setData(null); setError(null);
    const q = new URLSearchParams({ date_from: from, date_to: to, ...(station ? { station } : {}) });
    api(`/api/reports/dip-history/?${q}`).then(setData).catch((e) => setError(errorMessage(e)));
  }, [station, from, to]);

  return (
    <>
      <h1>Dip history</h1>
      <div className="row gap">
        {stations.length > 1 && (
          <label>Station<select value={station} onChange={(e) => setStation(e.target.value)}>
            <option value="">All stations</option>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select></label>
        )}
        <label>From<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label>
      </div>
      <div className="row between gap">
        <p className="muted">The Excel sheet lists every dip with a chart check (do the litres match the mm?) and a stock check from one day-close dip to the next.</p>
        <button className="btn" disabled={exporting} onClick={() => void exportSheet()}>{exporting ? "Preparing…" : "Download dip sheet (Excel)"}</button>
      </div>
      <ErrorNote message={error} />
      {!data && !error && <p className="muted center">Loading…</p>}
      {data && (
        <>
          <section className="card"><h2>Last dip per tank</h2>
            <p className="muted">Each tank should be dipped at least every {data.every_hours} hours.</p>
            {data.tanks.length === 0 && <p className="muted">No tanks yet.</p>}
            {data.tanks.map((t: any) => (
              <div key={t.id} className="row between gap hist-tank">
                <span><strong>{t.name}</strong> <span className="muted">· {t.station} · {t.fuel_code}</span><br />
                  <span className="muted">{t.last_dip_at ? `${new Date(t.last_dip_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })} by ${t.last_dip_by ?? "unknown"} · ${ago(t.hours_since_last_dip)}` : "Never dipped"}</span></span>
                <Badge kind={t.overdue ? "suspended" : "active"}>{t.overdue ? "dip overdue" : "on time"}</Badge>
              </div>
            ))}
          </section>
          {data.days.map((d: any) => (
            <section key={d.date} className={`card ${d.missing.length ? "bad" : ""}`}>
              <div className="row between"><h2>{dayName(d.date)}</h2><span className="muted">{(() => { const n = d.entries.filter((e: any) => e.type === "dip").length; return `${n} dip${n === 1 ? "" : "s"}`; })()}</span></div>
              {d.entries.length === 0 && <p className="muted">Nothing recorded.</p>}
              <ul className="plain hist">
                {d.entries.map((e: any, i: number) => (
                  <li key={i} className={e.type}>
                    <span className="hist-time">{time(e.at)}</span>
                    <span>{e.type === "dip"
                      ? <><strong>{e.tank}</strong>{data.tanks.length > 1 ? <span className="muted"> · {e.station}</span> : null}: dip {e.dip_mm ? `${e.dip_mm} mm = ` : ""}{Number(e.litres).toLocaleString()} L</>
                      : e.type === "correction"
                        ? <><strong>Day close of {dayName(e.close_day)} corrected</strong>{data.tanks.length > 1 ? <span className="muted"> · {e.station}</span> : null}:{" "}
                          {[...e.dips.map((x: any) => `${x.tank} ${dipText(x.from)} → ${dipText(x.to)}`), ...e.meters.map((x: any) => `${x.pump} meter ${x.from} → ${x.to}`)].join("; ")}
                          {e.note ? <span className="muted"> ({e.note})</span> : null}</>
                        : <><strong>Delivery</strong> to {e.tank}: {Number(e.litres).toLocaleString()} L from {e.supplier || "supplier not given"}{e.note_number ? ` (note ${e.note_number})` : ""}</>}
                      {e.by && <span className="muted"> · {e.by}</span>}</span>
                  </li>
                ))}
              </ul>
              {d.missing.length > 0 && <p className="warn">No dip: {d.missing.join(", ")}</p>}
            </section>
          ))}
        </>
      )}
    </>
  );
}
