import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";
import { DayReport, n, shortDay } from "./DayReport";

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
      <h1>Today</h1>
      <PumpGaps gaps={d.pump_gaps ?? []} />
      <DailyReports />
      <div className="row between"><h2>Right now</h2><span className="muted" data-testid="updated">Updated {new Date(d.as_of).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></div>
      <div className="grid">
        <div className="card stat"><span className="muted">Sold today</span><strong>{Number(t.today_litres).toLocaleString()} L</strong><span>{amt(t.today_amount_by_currency)}</span></div>
        <div className="card stat"><span className="muted">Next refill by</span><strong>{d.next_refill?.refill_by ?? "—"}</strong><span>{d.next_refill?.tank ?? "No forecast yet"}</span></div>
        <Link to="/alerts" className={`card stat ${t.open_alerts ? "bad" : ""}`}><span className="muted">Needs attention</span><strong>{t.open_alerts}</strong><span>{Number(t.unexplained_loss_7d) > 0 ? `${Number(t.unexplained_loss_7d).toFixed(0)} L unexplained (7 days)` : "No fuel loss flagged"}</span></Link>
        <Link to="/reports" className={`card stat ${t.flagged_cash_shifts_7d ? "bad" : ""}`}><span className="muted">Cash differences (7 days)</span><strong>{t.flagged_cash_shifts_7d}</strong><span>{t.open_shifts} shift{t.open_shifts === 1 ? "" : "s"} open now</span></Link>
      </div>
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

/** The latest closing report per station, at the top of the page: the trading day 21:00 to 21:00, named by the day it
 * closes, from the manager's day close. With several stations, a line at the top lists them and each report points to
 * the next one below. */
function DailyReports() {
  const [reports, setReports] = useState<any[] | null>(null);
  const [picked, setPicked] = useState("");
  const [station, setStation] = useState(() => { try { return localStorage.getItem(LAST_STATION) ?? ""; } catch { return ""; } });
  const touchX = useRef(0);
  useEffect(() => { void api<any[]>(`/api/reports/daily/${picked ? `?day=${picked}` : ""}`).then(setReports).catch(() => setReports([])); }, [picked]);
  if (!reports?.length) return null;
  const index = Math.max(0, reports.findIndex((r) => r.station === station));
  const r = reports[index];
  const choose = (i: number) => {
    const next = reports[(i + reports.length) % reports.length].station;
    setStation(next);
    try { localStorage.setItem(LAST_STATION, next); } catch { /* private mode: just not remembered */ }
  };
  const names = shortNames(reports.map((x) => x.station_name));
  return (<>
    <div className="row between"><h2>{picked ? "Closing report" : "Latest closing report"}</h2>
      <label className="row gap"><span className="muted">Day</span><input type="date" aria-label="Report day" value={picked || r.day} onChange={(e) => setPicked(e.target.value)} /></label></div>
    {reports.length > 1 && (
      <div className="station-tabs" role="tablist" aria-label="Station reports">
        {reports.map((x, i) => (
          <button key={x.station} role="tab" aria-selected={i === index} className={`station-tab ${i === index ? "on" : ""}`} onClick={() => choose(i)}>
            <strong>{names[i]}</strong>
            <span>{picked || x.closed ? `${shortDay(x.day)} · ${x.currency} ${n(x.totals.grand_total)}` : "No day closed yet"}</span>
          </button>
        ))}
      </div>
    )}
    {/* swipe left or right on the report to go to the other station */}
    <div onTouchStart={(e) => { touchX.current = e.touches[0].clientX; }}
      onTouchEnd={(e) => { const dx = e.changedTouches[0].clientX - touchX.current; if (reports.length > 1 && Math.abs(dx) > 60) choose(index + (dx < 0 ? 1 : -1)); }}>
      {picked || r.closed
        ? <DayReport r={r} of={{ index: index + 1, count: reports.length }} />
        : <p className="card muted">{r.station_name}: no day has been closed yet. The manager closes the day on the Dip screen at about 21:00.</p>}
    </div>
  </>);
}

/** Assigned pumps nobody is working, or nobody is planned for: the manager gives each one to someone on the Pumps
 * screen, otherwise the litres on it belong to nobody. Goes away as soon as every pump has someone. */
function PumpGaps({ gaps }: { gaps: any[] }) {
  if (!gaps.length) return null;
  const stations = [...new Set(gaps.map((g) => g.station_name))];
  return (
    <section className="card bad" role="alert" data-testid="pump-gaps">
      <h2>Pumps without an attendant</h2>
      {stations.map((name) => (
        <div key={name}>
          {stations.length > 1 && <strong>{name}</strong>}
          <ul>{gaps.filter((g) => g.station_name === name).map((g) => <li key={g.pump}>{g.message}</li>)}</ul>
        </div>
      ))}
      <Link to="/pumps" className="btn">Assign pumps</Link>
    </section>
  );
}

const LAST_STATION = "pumpwise.report.station"; // the station report last looked at, shown first next time

/** Station names without the words they all share ("Eastview Mutamba Fuel Zone" → "Eastview Mutamba"). */
function shortNames(names: string[]) {
  if (names.length < 2) return names;
  const words = names.map((x) => x.split(" "));
  let common = 0;
  while (words.every((w) => w.length > common + 1 && w[w.length - 1 - common] === words[0][words[0].length - 1 - common])) common++;
  return words.map((w) => w.slice(0, w.length - common).join(" "));
}
