import { useEffect, useState } from "react";
import { api, downloadFile, errorMessage } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorNote } from "../components";
import { DayReport } from "./DayReport";

const today = () => new Date().toISOString().slice(0, 10);
const daysAgo = (n: number) => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
// Neutral wording (PLAN.md section 4): a "cash difference", never an accusation.
const CASH_LABEL: Record<string, string> = { ok: "ok", short: "cash difference", over: "cash difference", incomplete: "rate missing", open: "open" };

export default function Reports() {
  const { user } = useAuth();
  const [tab, setTab] = useState<"days" | "sales" | "tank" | "cash" | "people" | "pumps">("days");
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
    const path = { days: "/api/reports/days/", sales: "/api/reports/daily-sales/", tank: "/api/reconciliation/tanks/", cash: "/api/reconciliation/cash/", people: "/api/reports/attendants/", pumps: "/api/reports/pumps/" }[tab];
    api(`${path}?date_from=${from}&date_to=${to}${station ? `&station=${station}` : ""}`).then((d) => setLoaded({ tab, data: d })).catch((e) => setError(errorMessage(e)));
  }, [tab, from, to, station]);
  return (
    <>
      <h1>Reports</h1>
      <div className="chips">{([["days", "By day"], ["sales", "Sales"], ["tank", "Fuel check"], ["cash", "Shifts: cash check"], ["people", "By attendant"], ["pumps", "Pumps"]] as const).map(([k, l]) => <button key={k} className={`chip ${tab === k ? "on" : ""}`} onClick={() => setTab(k)}>{l}</button>)}</div>
      <div className="filters">
        {stations.length > 1 && <label className="wide">Station<select value={station} onChange={(e) => setStation(e.target.value)}><option value="">All stations</option>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
        <label>From<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></label><label>To<input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></label></div>
      <ErrorNote message={error} />
      {user?.role === "owner" && <AccountingPack stations={stations} />}
      {!data && !error && <p className="muted center">Loading…</p>}
      {data && tab === "days" && (data.days.length ? (<>
        <p className="muted">Each trading day runs 21:00 to 21:00 and is named by the day it closes. Open a day's shifts for the detail.</p>
        {data.days.map((r: any) => <DayReport key={`${r.day}-${r.station}`} r={r} />)}</>)
        : <p className="muted center">No days in this period.</p>)}
      {data && tab === "sales" && (<>
        <section className="card">{station && <p className="muted">{stations.find((s) => s.id === station)?.name}</p>}<strong>{Number(data.totals.litres).toLocaleString()} L</strong> sold · {Object.entries(data.totals.amount_by_currency).map(([c, a]) => `${c} ${a}`).join(" · ")}
          <p className="muted">Average {Number(data.averages.avg_daily_litres).toLocaleString()} L per day</p>
          <div className="weekdays">{data.averages.by_weekday.map((w: any) => <div key={w.weekday} title={w.name}><div className="wbar" style={{ height: Math.max(4, Number(w.avg_litres) / Math.max(...data.averages.by_weekday.map((x: any) => Number(x.avg_litres)), 1) * 60) }} /><span>{w.name.slice(0, 3)}</span></div>)}</div></section>
        {[...data.days].reverse().map((d: any) => (
          <details key={d.date} className="card"><summary className="row between"><span>{d.date}</span><span>{Number(d.litres).toLocaleString()} L · {Object.entries(d.amount_by_currency).map(([c, a]) => `${c} ${a}`).join(" ") || "—"}</span></summary>
            {d.by_pump.map((p: any) => <p key={p.pump_id + p.currency} className="muted">{p.pump_name}: {Number(p.litres).toLocaleString()} L · {p.currency} {p.amount}</p>)}</details>
        ))}</>)}
      {data && tab === "tank" && (data.rows.length ? <FuelCheck rows={data.rows} totals={data.totals ?? []} />
        : <p className="muted center">No tank checks yet. They appear once a tank has been dipped on two occasions.</p>)}
      {data && tab === "cash" && (data.rows.length ? data.rows.map((r: any) => (
        <section key={r.shift_id} className={`card ${r.flagged ? "bad" : ""}`}><div className="row between"><strong>{r.station} · {(r.attendants?.length ? r.attendants : [r.attendant]).join(", ")}</strong><Badge kind={r.status === "ok" ? "active" : "suspended"}>{CASH_LABEL[r.status] ?? r.status}</Badge></div>
          <p className="muted">{new Date(r.started_at).toLocaleDateString()}</p>
          <p>Expected {r.expected} · handed in {r.submitted_total} {r.currency} · cash difference <strong>{r.variance}</strong></p>
          <p className="muted">{r.by_payment_type.map((l: any) => `${l.payment_type === "credit" ? "intercompany issue" : l.payment_type.replace("_", " ")} ${l.currency} ${l.amount}`).join(" · ")}</p>
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
      {data && tab === "pumps" && <PumpReport stations={data.stations} />}
    </>
  );
}

/** Owner only: the monthly accounting pack, one Excel workbook per station and month (sales, takings, cash differences, deliveries,
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

const litres = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 3 });
const signed = (v: string | number) => (Number(v) > 0 ? "+" : "") + litres(v);
const when = (iso: string) => new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const word = (v: number, status?: string) => (status === "ok" ? "within allowance" : v > 0 ? "loss: fuel missing" : v < 0 ? "gain: more fuel than the meters show" : "balanced");

/** Each tank's fuel checks, newest first. Every check shows its sum (opening dip + delivered − sold − closing dip); the
 * latest one also shows the running difference over the chosen dates and the daily differences that add up to it.
 * + is a loss (fuel missing), − a gain. A loss and the matching gain from a dip taken while pumps were in use cancel. */
function FuelCheck({ rows, totals }: { rows: any[]; totals: any[] }) {
  const tanks = [...new Map(rows.map((r) => [r.tank_id, r.tank])).entries()];
  return (<>
    <p className="muted">Each check: opening dip + delivered − sold (meters) − closing dip = difference. + is a loss (fuel missing), − a gain.</p>
    {tanks.map(([id, name]) => {
      let run = 0;
      const oldestFirst = rows.filter((r) => r.tank_id === id).sort((x, y) => x.date.localeCompare(y.date)).map((r) => ({ ...r, running: (run += Number(r.loss)) }));
      const total = totals.find((t) => t.tank_id === id);
      return (
        <section key={id} aria-label={`${name} fuel checks`}>
          <h2>{name}</h2>
          {[...oldestFirst].reverse().map((r, i) => (
            <section key={r.date} className={`card ${i === 0 && total ? (total.status === "gain" ? "good" : total.status === "loss" ? "bad" : "") : r.status === "gain" ? "good" : r.flagged ? "bad" : ""}`}>
              <div className="row between"><strong>{r.date}{i === 0 ? " · latest" : ""}</strong><Badge kind={r.status === "loss" ? "suspended" : "active"}>{r.status}</Badge></div>
              <p className="muted">Dips {when(r.window_start)} → {when(r.window_end)}</p>
              <p>{litres(r.opening)} + {litres(r.deliveries)} − {litres(r.dispensed)} − {litres(r.closing)} = <strong>{signed(r.loss)} L</strong> <span className="muted">({word(Number(r.loss), r.status)})</span></p>
              {i === 0 && total ? (
                <div aria-label={`${name} running difference`} role="region">
                  <p><strong>Running difference: {signed(total.unexplained)} L</strong> <span className="muted">({word(Number(total.unexplained), total.status)})</span></p>
                  <p className="muted">= {oldestFirst.map((x) => `${signed(x.loss)}`).join(" + ")} L · {litres(total.sold)} L sold{total.pct ? ` (${total.pct}%)` : ""} · allowed ±{litres(total.allowed)} L</p>
                </div>
              ) : <p className="muted">Running difference up to this check: {signed(r.running)} L</p>}
            </section>
          ))}
        </section>
      );
    })}
  </>);
}

/** Per day and night shift: how many of the station's pumps had an attendant (4/4) and who worked each one. A pump
 * nobody worked is marked, so the manager can see a shift where a pump was not allocated. */
function PumpReport({ stations }: { stations: any[] }) {
  if (!stations.length) return <p className="muted center">No stations.</p>;
  return (<>
    <p className="muted">Day shift 07:00 to 18:00, night shift 18:00 to 07:00. Every pump should have an attendant on every shift.</p>
    {stations.map((st) => (
      <section key={st.station} className="card">
        <h2>{st.station_name} <span className="muted">· {st.pumps} pump{st.pumps === 1 ? "" : "s"}{st.assigned ? "" : " · shared pumps"}</span></h2>
        <table className="table">
          <thead><tr><th>Shift</th><th>Pumps</th>{st.shifts[0]?.pumps.map((p: any) => <th key={p.pump}>{p.pump}</th>)}</tr></thead>
          <tbody>{st.shifts.map((r: any) => (
            <tr key={r.label}>
              <td>{r.label}{r.in_progress && <span className="muted"> (now)</span>}</td>
              <td><strong className={r.covered < r.total ? "bad-text" : "ok-note"}>{r.covered}/{r.total}</strong></td>
              {r.pumps.map((p: any) => <td key={p.pump}>{p.people.length ? p.people.join(", ") : <span className="bad-text">nobody</span>}</td>)}
            </tr>
          ))}</tbody>
        </table>
      </section>
    ))}
  </>);
}
