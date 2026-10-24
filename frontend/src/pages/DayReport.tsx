import type { ReactNode } from "react";

const longDay = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
export const shortDay = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
export const n = (v: string | number, digits = 2) => Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const when = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
// a meter at a day close, else the attendants' nearest reading with its time
const meter = (value: string | null, from: string | null) =>
  value == null ? "no reading" : from === "close" || from == null ? value : `${value} (attendant, ${when(from)})`;
export const closedText = (r: any) => r.closed ? `Closed ${clock(r.closed_at)}${r.closed_by ? ` by ${r.closed_by}` : ""}` : r.so_far ? "So far (not closed yet)" : "Not closed";

/** One section of the report: a heading row across the table, then its rows. */
function Section({ title, label, head, children }: { title: ReactNode; label: string; head?: [string, string, string]; children: ReactNode }) {
  return (
    <table className="report" aria-label={label}>
      <thead>
        <tr><th className="section" colSpan={3}>{title}</th></tr>
        {head && <tr className="sub"><td>{head[0]}</td><td className="num">{head[1]}</td><td className="num">{head[2]}</td></tr>}
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}

/** One station's trading day (21:00 to 21:00, named by the day it closes) as one underlined table that reads top to
 * bottom: money, cash losses, dips, sold, fuel check, pumps, attendants and shifts. Used on the Today dashboard and
 * in Reports. `of` says which station this is when there are several. */
export function DayReport({ r, of }: { r: any; of?: { index: number; count: number } }) {
  const cur = r.currency;
  return (
    <section id={`report-${r.station}`} className="card" aria-label={`Daily report ${r.station_name}`}>
      <div className="row between"><h2>{r.station_name}</h2>{of && of.count > 1 && <span className="muted">Station {of.index} of {of.count}</span>}</div>
      <p className="day-line"><strong>Date: {longDay(r.day)}</strong> <span className="muted">{closedText(r)}</span></p>

      <Section title="Money" label="Grand total">
        <tr className="total"><td>Grand Total</td><td /><td className="num">{cur} {n(r.totals.grand_total)}</td></tr>
        {r.handed_in.map((h: any) => (
          <tr key={h.payment_type}><td>{h.label}</td><td className="muted">{h.payment_type === "credit" && Number(h.amount) ? "owed by the other company" : ""}</td><td className="num">{cur} {n(h.amount)}</td></tr>
        ))}
      </Section>

      <Section title="Cash losses" label="Cash losses">
        <tr className="total"><td>Cash Losses Total</td><td /><td className="num">{cur} {n(r.cash_losses.total)}</td></tr>
        {r.cash_losses.by_attendant.length === 0 && <tr><td>Attendant losses</td><td className="muted">$1 and above</td><td className="num">{cur} {n(0)}</td></tr>}
        {r.cash_losses.by_attendant.map((a: any) => <tr key={a.attendant}><td>{a.attendant}</td><td className="muted">$1 and above</td><td className="num">{cur} {n(a.amount)}</td></tr>)}
        <tr><td>Milking errors</td><td className="muted">under $1{r.cash_losses.milking_count ? `, ${r.cash_losses.milking_count}` : ""}</td><td className="num">{cur} {n(r.cash_losses.milking)}</td></tr>
      </Section>

      <Section title={`Dips ${r.closed ? "at the day close" : "(latest)"}`} label="Dips" head={["Fuel / tank", "mm", "Litres"]}>
        {r.dips.map((f: any) => [
          <tr key={f.fuel_code} className="total"><td>{f.fuel}</td><td /><td className="num">{n(f.litres, 0)} L</td></tr>,
          ...f.tanks.map((t: any) => (
            <tr key={`${f.fuel_code}-${t.tank}`} className="sub"><td>{t.tank}</td><td className="num">{t.dip_mm != null ? `${n(t.dip_mm, 0)} mm` : "—"}</td><td className="num">{t.litres != null ? `${n(t.litres, 0)} L` : "no dip"}</td></tr>
          )),
        ])}
      </Section>

      <Section title="Sold" label="Sold" head={["Fuel", "Litres", "Amount"]}>
        {r.fuels.map((f: any) => <tr key={f.fuel_code}><td>{f.fuel}</td><td className="num">{n(f.litres, 0)} L</td><td className="num">{cur} {n(f.amount)}</td></tr>)}
        <tr className="total"><td>Total</td><td className="num">{n(r.totals.litres, 0)} L</td><td className="num">{cur} {n(r.totals.amount)}</td></tr>
      </Section>

      {(r.tanks.length > 0 || r.meter_checks?.length > 0) && (
        <Section title="Fuel check" label="Fuel check" head={["Tank", "Opening + delivered − sold − closing", "Difference"]}>
          {/* each tank once: from the pump meters at the two day closes when both exist, else from the attendants' readings */}
          {[...(r.meter_checks ?? []), ...r.tanks.filter((t: any) => !r.meter_checks?.some((m: any) => m.tank === t.tank))].map((t: any) => (
            <tr key={t.tank} className={t.status === "loss" ? "warn" : undefined}><td>{t.tank}</td>
              <td className="num">{n(t.opening, 0)} + {n(t.deliveries, 0)} − {n(t.dispensed, 0)} − {n(t.closing, 0)}</td>
              <td className="num">{Number(t.loss) > 0 ? "+" : ""}{n(t.loss, 1)} L</td></tr>
          ))}
        </Section>
      )}

      <Section title="Pumps" label="Pumps" head={["Pump", "Opening → closing meter", "Litres"]}>
        {r.pumps.map((p: any) => (
          <tr key={`${p.pump}-${p.nozzle}`}><td>{p.pump} · {p.nozzle}</td><td className="num">{meter(p.opening_meter, p.opening_from)} → {meter(p.closing_meter, p.closing_from)}</td>
            <td className="num">{p.meter_litres ? `${n(p.meter_litres, 0)} L` : "—"}</td></tr>
        ))}
      </Section>
      {r.pumps.some((p: any) => p.opening_from !== "close" || p.closing_from !== "close") && <p className="muted">Without a day close, the nearest attendant reading is shown with its time.</p>}

      {r.people.length > 0 && (
        <Section title="Attendants" label="Attendants" head={["Attendant (shift from)", "Litres", "Amount"]}>
          {r.people.map((p: any) => (
            <tr key={`${p.shift_id}-${p.attendant}`}><td>{p.attendant} <span className="muted">({when(p.shift_started_at)})</span></td><td className="num">{n(p.litres, 0)} L</td><td className="num">{cur} {n(p.amount)}</td></tr>
          ))}
        </Section>
      )}

      {r.shifts?.length > 0 && (
        <Section title={`Shifts (${r.shifts.length})`} label="Shifts" head={["Shift", "Expected / handed in", "Difference"]}>
          {r.shifts.map((s: any) => [
            <tr key={s.key ?? s.shift_id} aria-label={`Shift ${s.attendants.join(", ")}`}>
              <td>{s.label && <><strong>{s.label}</strong><br /></>}{s.attendants.join(", ") || "Not assigned"}<br /><span className="muted">{when(s.started_at)} → {s.ended_at ? when(s.ended_at) : "still working"}</span></td>
              <td className="num">{s.expected != null ? `${n(s.expected)} / ${n(s.handed_in)}` : `— / ${n(s.handed_in)}`}</td>
              <td className="num"><strong>{s.cash_difference != null ? n(s.cash_difference) : "—"}</strong></td>
            </tr>,
            ...(s.waiting ?? []).map((w: any) => (
              <tr key={`${s.key ?? s.shift_id}-${w.name}`} className="sub"><td>Meters closed, cash not in yet: {w.name}</td><td className="num">{w.expected != null ? `owes ${cur} ${n(w.expected)}` : ""}</td><td /></tr>
            )),
          ])}
        </Section>
      )}
    </section>
  );
}
