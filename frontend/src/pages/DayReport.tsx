const longDay = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const n = (v: string | number, digits = 2) => Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
// a meter at a day close, else the attendants' nearest reading with its time
const meter = (value: string | null, from: string | null) =>
  value == null ? "no reading" : from === "close" || from == null ? value : `${value} (attendant, ${new Date(from).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })})`;
const when = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Scroll to a station's report (the reports sit on one page, one below the other). */
export const jumpToReport = (station: string) => document.getElementById(`report-${station}`)?.scrollIntoView({ behavior: "smooth" });

/** One station's trading day (21:00 to 21:00, named by the day it closes), as one report that reads top to bottom
 * under headings: money, cash losses, dips, sold, fuel check, pumps, attendants and shifts. Used on the Today
 * dashboard and in Reports. With several stations, `of` says which one this is and `next` points to the one below. */
export function DayReport({ r, of, next }: { r: any; of?: { index: number; count: number }; next?: { id: string; name: string } }) {
  return (
    <section id={`report-${r.station}`} className="card" aria-label={`Daily report ${r.station_name}`}>
      <div className="row between"><h2>{r.station_name}</h2>{of && of.count > 1 && <span className="muted">Station {of.index} of {of.count}</span>}</div>
      <div className="row between"><strong>Date: {longDay(r.day)}</strong>
        <span className="muted">{r.closed ? `closed ${new Date(r.closed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}${r.closed_by ? ` by ${r.closed_by}` : ""}` : r.so_far ? "so far (not closed yet)" : "not closed"}</span></div>

      <h3>Money</h3>
      <table className="plain" aria-label="Grand total"><tbody>
        <tr><td><strong>Grand Total</strong></td><td><strong>{r.currency} {n(r.totals.grand_total)}</strong></td></tr>
        {r.handed_in.map((h: any) => <tr key={h.payment_type}><td>{h.label}{h.payment_type === "credit" && Number(h.amount) ? <span className="muted"> (owed by the other company)</span> : null}</td><td>{r.currency} {n(h.amount)}</td></tr>)}
      </tbody></table>

      <h3>Cash losses</h3>
      <table className="plain" aria-label="Cash losses"><tbody>
        <tr><td><strong>Cash Losses Total</strong></td><td><strong>{r.currency} {n(r.cash_losses.total)}</strong></td></tr>
        {r.cash_losses.by_attendant.length === 0 && <tr><td className="muted">Attendant losses ($1 and above)</td><td>{r.currency} {n(0)}</td></tr>}
        {r.cash_losses.by_attendant.map((a: any) => <tr key={a.attendant}><td>{a.attendant}</td><td>{r.currency} {n(a.amount)}</td></tr>)}
        <tr><td>Milking errors <span className="muted">(under $1{r.cash_losses.milking_count ? `, ${r.cash_losses.milking_count}` : ""})</span></td><td>{r.currency} {n(r.cash_losses.milking)}</td></tr>
      </tbody></table>

      <h3>Dips <span className="muted">{r.closed ? "at the day close" : "latest"}</span></h3>
      <table className="plain" aria-label="Dips"><tbody>
        {r.dips.map((f: any) => [
          <tr key={f.fuel_code}><td>{f.fuel}</td><td /><td>{n(f.litres, 0)} L</td></tr>,
          ...(f.tanks.length > 1 || f.tanks[0]?.dip_mm ? f.tanks.map((t: any) => <tr key={`${f.fuel_code}-${t.tank}`} className="muted"><td>· {t.tank}</td><td>{t.dip_mm != null ? `${n(t.dip_mm, 0)} mm` : "—"}</td><td>{t.litres != null ? `${n(t.litres, 0)} L` : "no dip"}</td></tr>) : []),
        ])}
      </tbody></table>

      <h3>Sold</h3>
      <table className="plain" aria-label="Sold"><tbody>
        {r.fuels.map((f: any) => <tr key={f.fuel_code}><td>{f.fuel}</td><td>{n(f.litres, 0)} L</td><td>{r.currency} {n(f.amount)}</td></tr>)}
        <tr><td><strong>Total</strong></td><td>{n(r.totals.litres, 0)} L</td><td>{r.currency} {n(r.totals.amount)}</td></tr>
      </tbody></table>

      {r.tanks.length > 0 && (<>
        <h3>Fuel check</h3>
        {r.tanks.map((t: any) => (
          <p key={t.tank} className={t.status === "loss" ? "warn" : "muted"}>{t.tank}: {n(t.opening, 0)} + {n(t.deliveries, 0)} − {n(t.dispensed, 0)} − {n(t.closing, 0)} = {Number(t.loss) > 0 ? "+" : ""}{n(t.loss, 1)} L</p>
        ))}
      </>)}

      <h3>Pumps</h3>
      <ul className="plain" aria-label="Pumps">{r.pumps.map((p: any) => <li key={`${p.pump}-${p.nozzle}`}>{p.pump} · {p.nozzle}: {meter(p.opening_meter, p.opening_from)} → {meter(p.closing_meter, p.closing_from)}{p.meter_litres ? ` = ${n(p.meter_litres, 0)} L` : ""}</li>)}</ul>
      {r.pumps.some((p: any) => p.opening_from !== "close" || p.closing_from !== "close") && <p className="muted">Without a day close, the nearest attendant reading is shown with its time.</p>}

      {r.people.length > 0 && (<>
        <h3>Attendants</h3>
        <ul className="plain" aria-label="Attendants">{r.people.map((p: any) => <li key={`${p.shift_id}-${p.attendant}`}>{p.attendant} (shift from {new Date(p.shift_started_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}): {n(p.litres, 0)} L · {r.currency} {n(p.amount)}</li>)}</ul>
      </>)}

      {r.shifts?.length > 0 && (<>
        <h3>Shifts ({r.shifts.length})</h3>
        {r.shifts.map((s: any) => (
          <div key={s.shift_id} className="card" aria-label={`Shift ${s.attendants.join(", ")}`}>
            <div className="row between"><strong>{s.attendants.join(", ") || "Not assigned"}</strong><span className="muted">{when(s.started_at)} → {s.ended_at ? when(s.ended_at) : "open"}</span></div>
            {s.waiting?.length ? (<>
              <p>Shift still open. Cash difference of those who handed in: <strong>{n(s.cash_difference)}</strong></p>
              <p className="muted">Still to hand in: {s.waiting.map((w: any) => `${w.name} (${r.currency} ${n(w.expected ?? 0)} so far)`).join(", ")}</p>
            </>) : <p>Expected {r.currency} {n(s.expected)} · handed in {n(s.handed_in)} · cash difference <strong>{n(s.cash_difference)}</strong></p>}
            {s.per_attendant_expected && s.by_attendant.length > 1 && <ul className="plain">{s.by_attendant.map((p: any) => <li key={p.user}>{p.name}: expected {n(p.expected ?? 0)} · handed in {n(p.submitted)} · difference {n(p.cash_difference ?? 0)}</li>)}</ul>}
            <ul className="plain muted">{s.by_pump.map((x: any) => <li key={`${x.pump_id}-${x.fuel_code}-${x.attendant}`}>{x.pump} {x.fuel_code} · {x.attendant_name}: {n(x.litres, 0)} L · {r.currency} {n(x.amount)}</li>)}</ul>
          </div>
        ))}
      </>)}

      {next && <p><a href={`#report-${next.id}`} onClick={(e) => { e.preventDefault(); jumpToReport(next.id); }}>Next: {next.name} report below ↓</a></p>}
    </section>
  );
}
