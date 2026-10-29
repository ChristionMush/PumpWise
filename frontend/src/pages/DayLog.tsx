import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import { shortDay } from "./DayReport";

const time = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
const shiftDay = (iso: string, by: number) => { const d = new Date(`${iso}T12:00:00`); d.setDate(d.getDate() + by); return d.toISOString().slice(0, 10); };

/** Owner: what happened at a station during one trading day, in time order, opened from "What happened" on a daily
 * report. Points worth a look come first. */
export default function DayLog() {
  const [params, setParams] = useSearchParams();
  const station = params.get("station") ?? "";
  const day = params.get("day") ?? "";
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null); setError(null);
    api(`/api/reports/day-log/?${new URLSearchParams({ station, day })}`).then(setData).catch((e) => setError(errorMessage(e)));
  }, [station, day]);
  const go = (by: number) => setParams({ station, day: shiftDay(day, by) });

  return (
    <>
      <p><Link to="/">← Back to the report</Link></p>
      <h1>What happened</h1>
      <div className="row between">
        <button className="btn" onClick={() => go(-1)}>← Day before</button>
        <strong>{day && shortDay(day)}</strong>
        <button className="btn" onClick={() => go(1)}>Day after →</button>
      </div>
      <ErrorNote message={error} />
      {!data && !error && <p className="muted center">Loading…</p>}
      {data && (<>
        <p className="muted">{data.station_name} · trading day from {time(data.start)} to {time(data.end)}{data.in_progress ? " (still running)" : ""}</p>
        {data.highlights.length > 0 && (
          <section className="card bad" role="alert"><h2>Worth a look</h2>
            <ul>{data.highlights.map((h: string) => <li key={h}>{h}</li>)}</ul></section>
        )}
        <section className="card">
          {data.events.length ? (
            <table className="report" aria-label="What happened">
              <tbody>{data.events.map((e: any, i: number) => (
                <tr key={i}><td className="muted num">{time(e.at)}</td><td>{e.text}{e.by && !e.text.startsWith(e.by) && <span className="muted"> · {e.by}</span>}</td></tr>
              ))}</tbody>
            </table>
          ) : <p className="muted">Nothing was recorded on this day.</p>}
        </section>
      </>)}
    </>
  );
}
