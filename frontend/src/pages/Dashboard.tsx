import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";

const amt = (o: Record<string, string>) => Object.entries(o).map(([c, a]) => `${c} ${Number(a).toLocaleString(undefined, { minimumFractionDigits: 2 })}`).join(" · ") || "—";

export default function Dashboard() {
  const [d, setD] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    // a failed refresh shows the error only until the next one succeeds
    const load = () => api("/api/dashboard/").then((data) => { setD(data); setError(null); }).catch((e) => setError(errorMessage(e)));
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, []);
  if (error) return <ErrorNote message={error} />;
  if (!d) return <p className="muted center">Loading…</p>;
  const t = d.totals;
  return (
    <>
      <div className="row between"><h1>Today</h1><span className="muted" data-testid="updated">Updated {new Date(d.as_of).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>
      <div className="grid">
        <div className="card stat"><span className="muted">Sold today</span><strong>{Number(t.today_litres).toLocaleString()} L</strong><span>{amt(t.today_amount_by_currency)}</span></div>
        <div className="card stat"><span className="muted">Next refill by</span><strong>{d.next_refill?.refill_by ?? "—"}</strong><span>{d.next_refill?.tank ?? "No forecast yet"}</span></div>
        <Link to="/alerts" className={`card stat ${t.open_alerts ? "bad" : ""}`}><span className="muted">Needs attention</span><strong>{t.open_alerts}</strong><span>{Number(t.unexplained_loss_7d) > 0 ? `${Number(t.unexplained_loss_7d).toFixed(0)} L unexplained (7 days)` : "No fuel loss flagged"}</span></Link>
        <Link to="/reports" className={`card stat ${t.flagged_cash_shifts_7d ? "bad" : ""}`}><span className="muted">Cash differences (7 days)</span><strong>{t.flagged_cash_shifts_7d}</strong><span>{t.open_shifts} shift{t.open_shifts === 1 ? "" : "s"} open now</span></Link>
      </div>
      <DailyReports />
      {d.stations.map((s: any) => (
        <section key={s.id} className="card"><h2>{s.name}</h2>
          <p>{Number(s.today.litres).toLocaleString()} L · {amt(s.today.amount_by_currency)}</p>
          {s.tanks.map((k: any) => k.status === "ok" ? (
            <div key={k.tank_id} className="tank"><div className="row between"><span>{k.tank} <span className="muted">{k.fuel_code}</span></span><span>{Number(k.current_litres).toLocaleString()} L {k.low && <Badge kind="suspended">low</Badge>}</span></div>
              <div className="bar"><div className={k.low ? "fill low" : "fill"} style={{ width: `${Math.min(k.level_pct, 100)}%` }} /></div>
              <span className="muted">{k.days_to_empty != null ? `${k.days_to_empty} days left · order by ${k.refill_by}` : "Not enough sales history to forecast"}</span></div>
          ) : <p key={k.tank_id} className="muted">{k.tank}: no dip recorded yet</p>)}
          {s.open_shifts.map((o: any) => <p key={o.id} className="muted">Shift open: {(o.attendants?.length ? o.attendants : [o.attendant]).filter(Boolean).join(", ") || "no one yet"} for {o.hours_open} h</p>)}
        </section>
      ))}
    </>
  );
}

const day = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
const n = (v: string | number, digits = 2) => Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** The daily report per station: the trading day 21:00 to 21:00, named by the day it closes, from the manager's day
 * close. Shows the last closed day; the shifts and attendants are the deep dive. */
function DailyReports() {
  const [reports, setReports] = useState<any[] | null>(null);
  const [picked, setPicked] = useState("");
  useEffect(() => { void api<any[]>(`/api/reports/daily/${picked ? `?day=${picked}` : ""}`).then(setReports).catch(() => setReports([])); }, [picked]);
  if (!reports?.length) return null;
  return (<>
    <div className="row between"><h2>Daily report</h2>
      <label className="row gap"><span className="muted">Day</span><input type="date" aria-label="Report day" value={picked || reports[0].day} onChange={(e) => setPicked(e.target.value)} /></label></div>
    {reports.map((r) => (
      <section key={r.station} className="card" aria-label={`Daily report ${r.station_name}`}>
        <div className="row between"><strong>{r.station_name} · {day(r.day)}</strong>
          <span className="muted">{r.closed ? `closed ${new Date(r.closed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}${r.closed_by ? ` by ${r.closed_by}` : ""}` : r.so_far ? "so far (not closed yet)" : "not closed"}</span></div>
        <table className="plain"><tbody>
          {r.fuels.map((f: any) => <tr key={f.fuel_code}><td>{f.fuel}</td><td>{n(f.litres, 0)} L</td><td>{r.currency} {n(f.amount)}</td></tr>)}
          <tr><td><strong>Total</strong></td><td><strong>{n(r.totals.litres, 0)} L</strong></td><td><strong>{r.currency} {n(r.totals.amount)}</strong></td></tr>
        </tbody></table>
        {r.handed_in.length > 0 && <p className="muted">Handed in: {r.handed_in.map((h: any) => `${h.label} ${n(h.amount)}`).join(" · ")}{Number(r.totals.intercompany) ? ` (intercompany ${n(r.totals.intercompany)} is owed by the other company)` : ""}</p>}
        {r.tanks.map((t: any) => (
          <p key={t.tank} className={t.status === "loss" ? "warn" : "muted"}>{t.tank}: {n(t.opening, 0)} + {n(t.deliveries, 0)} − {n(t.dispensed, 0)} − {n(t.closing, 0)} = {Number(t.loss) > 0 ? "+" : ""}{n(t.loss, 1)} L</p>
        ))}
        <details><summary>Deep dive: pumps and shifts</summary>
          <ul className="plain">{r.pumps.map((p: any) => <li key={`${p.pump}-${p.nozzle}`}>{p.pump} · {p.nozzle}: {p.opening_meter ?? "?"} → {p.closing_meter ?? "?"}{p.meter_litres ? ` = ${n(p.meter_litres, 0)} L` : ""}</li>)}</ul>
          <ul className="plain">{r.people.map((p: any) => <li key={`${p.shift_id}-${p.attendant}`}>{p.attendant} (shift from {new Date(p.shift_started_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}): {n(p.litres, 0)} L · {r.currency} {n(p.amount)}</li>)}</ul>
        </details>
      </section>
    ))}
  </>);
}
