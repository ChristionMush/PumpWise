import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";

const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (n: number) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);

export default function Reports() {
  const [tab, setTab] = useState<"sales" | "tank" | "cash">("sales");
  const [from, setFrom] = useState(daysAgo(13));
  const [to, setTo] = useState(today());
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setError(null);
    const path = { sales: "/api/reports/daily-sales/", tank: "/api/reconciliation/tanks/", cash: "/api/reconciliation/cash/" }[tab];
    api(`${path}?date_from=${from}&date_to=${to}`).then(setData).catch((e) => setError(errorMessage(e)));
  }, [tab, from, to]);
  return (
    <>
      <h1>Reports</h1>
      <div className="chips">{([["sales", "Sales"], ["tank", "Fuel check"], ["cash", "Cash check"]] as const).map(([k, l]) => <button key={k} className={`chip ${tab === k ? "on" : ""}`} onClick={() => setTab(k)}>{l}</button>)}</div>
      <div className="row gap"><label>From<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label><label>To<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label></div>
      <ErrorNote message={error} />
      {!data && !error && <p className="muted center">Loading…</p>}
      {data && tab === "sales" && (<>
        <section className="card"><strong>{Number(data.totals.litres).toLocaleString()} L</strong> sold · {Object.entries(data.totals.amount_by_currency).map(([c, a]) => `${c} ${a}`).join(" · ")}
          <p className="muted">Average {Number(data.averages.avg_daily_litres).toLocaleString()} L per day</p>
          <div className="weekdays">{data.averages.by_weekday.map((w: any) => <div key={w.weekday} title={w.name}><div className="wbar" style={{ height: Math.max(4, Number(w.avg_litres) / Math.max(...data.averages.by_weekday.map((x: any) => Number(x.avg_litres)), 1) * 60) }} /><span>{w.name.slice(0, 3)}</span></div>)}</div></section>
        {[...data.days].reverse().map((d: any) => (
          <details key={d.date} className="card"><summary className="row between"><span>{d.date}</span><span>{Number(d.litres).toLocaleString()} L · {Object.entries(d.amount_by_currency).map(([c, a]) => `${c} ${a}`).join(" ") || "—"}</span></summary>
            {d.by_pump.map((p: any) => <p key={p.pump_id + p.currency} className="muted">{p.pump_name}: {Number(p.litres).toLocaleString()} L · {p.currency} {p.amount}</p>)}</details>
        ))}</>)}
      {data && tab === "tank" && (data.rows.length ? data.rows.map((r: any) => (
        <section key={r.tank_id + r.date} className={`card ${r.flagged ? "bad" : ""}`}><div className="row between"><strong>{r.tank} · {r.date}</strong><Badge kind={r.status === "ok" ? "active" : "suspended"}>{r.status}</Badge></div>
          <p className="muted">Opening {r.opening} + delivered {r.deliveries} − sold {r.dispensed} − closing {r.closing}</p>
          <p>Unexplained: <strong>{r.loss} L</strong>{r.loss_pct && ` (${r.loss_pct}%)`}</p></section>
      )) : <p className="muted center">No tank checks yet. They appear once a tank has been dipped on two occasions.</p>)}
      {data && tab === "cash" && (data.rows.length ? data.rows.map((r: any) => (
        <section key={r.shift_id} className={`card ${r.flagged ? "bad" : ""}`}><div className="row between"><strong>{r.station} · {r.attendant}</strong><Badge kind={r.status === "ok" ? "active" : "suspended"}>{r.status}</Badge></div>
          <p className="muted">{new Date(r.started_at).toLocaleDateString()}</p>
          <p>Expected {r.expected} · handed in {r.submitted_total} {r.currency} · difference <strong>{r.variance}</strong></p>
          <p className="muted">{r.by_payment_type.map((l: any) => `${l.payment_type.replace("_", " ")} ${l.currency} ${l.amount}`).join(" · ")}</p></section>
      )) : <p className="muted center">No closed shifts in this period.</p>)}
    </>
  );
}
