import { useEffect, useState } from "react";
import { api, downloadFile, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";

const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (n: number) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
// Neutral wording (PLAN.md section 4): a "cash difference", never an accusation.
const CASH_LABEL: Record<string, string> = { ok: "ok", short: "cash difference", over: "cash difference", incomplete: "rate missing", open: "open" };

export default function Reports() {
  const [tab, setTab] = useState<"sales" | "tank" | "cash" | "people">("sales");
  const [from, setFrom] = useState(daysAgo(13));
  const [to, setTo] = useState(today());
  const [stations, setStations] = useState<{ id: string; name: string }[]>([]);
  const [station, setStation] = useState(""); // "" = all the stations this person sees
  useEffect(() => { api<{ results: { id: string; name: string }[] }>("/api/stations/").then((r) => setStations(r.results)).catch(() => undefined); }, []);
  // Data is kept with the tab it belongs to: right after a tab switch the old tab's data must not be drawn as the new one.
  const [loaded, setLoaded] = useState<{ tab: string; data: any } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const data = loaded?.tab === tab ? loaded.data : null;
  useEffect(() => {
    setLoaded(null); setError(null);
    const path = { sales: "/api/reports/daily-sales/", tank: "/api/reconciliation/tanks/", cash: "/api/reconciliation/cash/", people: "/api/reports/attendants/" }[tab];
    api(`${path}?date_from=${from}&date_to=${to}${station ? `&station=${station}` : ""}`).then((d) => setLoaded({ tab, data: d })).catch((e) => setError(errorMessage(e)));
  }, [tab, from, to, station]);
  return (
    <>
      <h1>Reports</h1>
      <div className="chips">{([["sales", "Sales"], ["tank", "Fuel check"], ["cash", "Cash check"], ["people", "By attendant"]] as const).map(([k, l]) => <button key={k} className={`chip ${tab === k ? "on" : ""}`} onClick={() => setTab(k)}>{l}</button>)}</div>
      <div className="row gap">
        {stations.length > 1 && <label>Station<select value={station} onChange={(e) => setStation(e.target.value)}><option value="">All stations</option>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
        <label>From<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label><label>To<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label></div>
      <ErrorNote message={error} />
      <AccountingPack stations={stations} />
      {!data && !error && <p className="muted center">Loading…</p>}
      {data && tab === "sales" && (<>
        <section className="card">{station && <p className="muted">{stations.find((s) => s.id === station)?.name}</p>}<strong>{Number(data.totals.litres).toLocaleString()} L</strong> sold · {Object.entries(data.totals.amount_by_currency).map(([c, a]) => `${c} ${a}`).join(" · ")}
          <p className="muted">Average {Number(data.averages.avg_daily_litres).toLocaleString()} L per day</p>
          <div className="weekdays">{data.averages.by_weekday.map((w: any) => <div key={w.weekday} title={w.name}><div className="wbar" style={{ height: Math.max(4, Number(w.avg_litres) / Math.max(...data.averages.by_weekday.map((x: any) => Number(x.avg_litres)), 1) * 60) }} /><span>{w.name.slice(0, 3)}</span></div>)}</div></section>
        {[...data.days].reverse().map((d: any) => (
          <details key={d.date} className="card"><summary className="row between"><span>{d.date}</span><span>{Number(d.litres).toLocaleString()} L · {Object.entries(d.amount_by_currency).map(([c, a]) => `${c} ${a}`).join(" ") || "—"}</span></summary>
            {d.by_pump.map((p: any) => <p key={p.pump_id + p.currency} className="muted">{p.pump_name}: {Number(p.litres).toLocaleString()} L · {p.currency} {p.amount}</p>)}</details>
        ))}</>)}
      {data && tab === "tank" && (data.rows.length ? data.rows.map((r: any) => (
        <section key={r.tank_id + r.date} className={`card ${r.status === "gain" ? "good" : r.flagged ? "bad" : ""}`}><div className="row between"><strong>{r.tank} · {r.date}</strong><Badge kind={r.status === "ok" || r.status === "gain" ? "active" : "suspended"}>{r.status}</Badge></div>
          <p className="muted">Opening {r.opening} + delivered {r.deliveries} − sold {r.dispensed} − closing {r.closing}</p>
          <p>Unexplained: <strong>{r.loss} L</strong>{r.loss_pct && ` (${r.loss_pct}%)`}</p></section>
      )) : <p className="muted center">No tank checks yet. They appear once a tank has been dipped on two occasions.</p>)}
      {data && tab === "cash" && (data.rows.length ? data.rows.map((r: any) => (
        <section key={r.shift_id} className={`card ${r.flagged ? "bad" : ""}`}><div className="row between"><strong>{r.station} · {(r.attendants?.length ? r.attendants : [r.attendant]).join(", ")}</strong><Badge kind={r.status === "ok" ? "active" : "suspended"}>{CASH_LABEL[r.status] ?? r.status}</Badge></div>
          <p className="muted">{new Date(r.started_at).toLocaleDateString()}</p>
          <p>Expected {r.expected} · handed in {r.submitted_total} {r.currency} · cash difference <strong>{r.variance}</strong></p>
          <p className="muted">{r.by_payment_type.map((l: any) => `${l.payment_type.replace("_", " ")} ${l.currency} ${l.amount}`).join(" · ")}</p>
          {r.by_attendant?.length > 1 || r.per_attendant_expected ? (
            <ul className="plain">{r.by_attendant.map((p: any) => (
              <li key={p.user}>{p.name}: handed in {p.submitted}{r.per_attendant_expected ? ` · expected ${p.expected} · cash difference ${p.cash_difference}` : ""}
                {p.periods?.length ? <span className="muted"> ({p.periods.map((x: any) => x.pump).join(", ")})</span> : null}</li>
            ))}</ul>
          ) : null}
          {r.by_pump?.length ? (
            <ul className="plain muted">{r.by_pump.map((x: any) => (
              <li key={`${x.pump_id}-${x.fuel_code}-${x.attendant}`}>{x.pump} {x.fuel_code} · {x.attendant_name}: {Number(x.litres).toLocaleString()} L · {r.currency} {x.amount}</li>
            ))}</ul>
          ) : null}</section>
      )) : <p className="muted center">No closed shifts in this period.</p>)}
      {data && tab === "people" && (data.rows.length ? data.rows.map((r: any) => (
        <section key={r.user} className={`card ${r.flagged_shifts ? "bad" : ""}`}>
          <div className="row between"><strong>{r.name}</strong><span className="muted">{r.shifts} shift{r.shifts === 1 ? "" : "s"}</span></div>
          <p>Sold {Number(r.litres).toLocaleString()} L ({r.sales}) · handed in {r.handed_in}
            {r.cash_difference !== null ? <> · cash difference <strong>{r.cash_difference}</strong></> : <span className="muted"> · shared pumps on {r.shared_shifts} shift{r.shared_shifts === 1 ? "" : "s"}: no cash difference per person</span>}</p>
          {r.flagged_shifts ? <p className="muted">{r.flagged_shifts} shift{r.flagged_shifts === 1 ? "" : "s"} with a cash difference outside the allowed amount</p> : null}
          <table className="table"><thead><tr><th>Pump</th><th>Fuel</th><th>Litres</th><th>Sales</th></tr></thead>
            <tbody>{r.pumps.map((p: any) => <tr key={p.pump + p.fuel_code}><td>{p.pump}</td><td>{p.fuel_code}</td><td>{Number(p.litres).toLocaleString()}</td><td>{p.amount}</td></tr>)}</tbody></table>
        </section>
      )) : <p className="muted center">No closed shifts in this period.</p>)}
    </>
  );
}

/** The monthly accounting pack: one Excel workbook per station and month (sales, takings, cash differences, deliveries,
 * stock and prices), for the bookkeeper. */
function AccountingPack({ stations }: { stations: { id: string; name: string }[] }) {
  const [month, setMonth] = useState(new Date().toISOString().slice(0, 7));
  const [station, setStation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chosen = station || stations[0]?.id || "";
  const download = async () => {
    setBusy(true); setError(null);
    try {
      const name = stations.find((s) => s.id === chosen)?.name ?? "station";
      await downloadFile(`/api/reports/accounting-pack/?station=${chosen}&month=${month}`, `Accounting pack - ${name} - ${month}.xlsx`);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="card">
      <summary><strong>Accounting pack (Excel)</strong> <span className="muted">· one month, one station</span></summary>
      <p className="muted">Sales, takings, cash differences, deliveries, stock and prices for the bookkeeper.</p>
      <div className="row gap">
        {stations.length > 1 && <label>Station<select value={chosen} onChange={(e) => setStation(e.target.value)}>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
        <label>Month<input type="month" value={month} onChange={(e) => setMonth(e.target.value)} /></label>
      </div>
      <button className="btn primary" disabled={busy || !chosen} onClick={() => void download()}>{busy ? "Preparing…" : "Download accounting pack"}</button>
      <ErrorNote message={error} />
    </details>
  );
}
