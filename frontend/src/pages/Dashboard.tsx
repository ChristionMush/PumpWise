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
