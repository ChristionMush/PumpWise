import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import { shortDay } from "./DayReport";

const longDay = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const when = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
const shiftDay = (iso: string, by: number) => { const d = new Date(`${iso}T12:00:00`); d.setDate(d.getDate() + by); return d.toISOString().slice(0, 10); };

/** The shift a moment falls in: day 07:00-18:00, night 18:00-07:00 named by the evening it starts. */
function shiftOf(iso: string) {
  const d = new Date(iso);
  const day = d.getHours() >= 7 && d.getHours() < 18;
  const named = day ? d : new Date(d.getTime() - 7 * 36e5); // a night shift is named by its evening
  return `${day ? "Day" : "Night"} shift · ${named.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}`;
}

/** Owner: what happened at a station during one trading day, newest first and grouped by shift, opened from
 * "What happened this day" on a daily report. Points worth a look come first. Laid out like the daily report. */
export default function DayLog() {
  const navigate = useNavigate();
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

  const groups: { label: string; events: any[] }[] = [];
  for (const e of [...(data?.events ?? [])].reverse()) { // newest first: the page opens on the latest time
    const label = shiftOf(e.at);
    if (groups[groups.length - 1]?.label !== label) groups.push({ label, events: [] });
    groups[groups.length - 1].events.push(e);
  }

  return (
    <>
      <div className="row between"><h1>What happened</h1><button className="link" onClick={() => navigate(-1)}>Back</button></div>
      <div className="chips day-nav">
        <button className="chip" onClick={() => go(-1)}>‹ {day && shortDay(shiftDay(day, -1))}</button>
        <button className="chip on" disabled>{day && shortDay(day)}</button>
        <button className="chip" onClick={() => go(1)}>{day && shortDay(shiftDay(day, 1))} ›</button>
      </div>
      <ErrorNote message={error} />
      {!data && !error && <p className="muted center">Loading…</p>}
      {data && (
        <section className="card" aria-label={`What happened at ${data.station_name}`}>
          <h2>{data.station_name}</h2>
          <p className="day-line"><strong>Date: {longDay(data.day)}</strong> <span className="muted">{when(data.start)} to {when(data.end)}{data.in_progress ? " · still running" : ""}</span></p>

          {data.highlights.length > 0 && (
            <table className="report" aria-label="Worth a look">
              <thead><tr><th className="section attention" colSpan={2}>Worth a look</th></tr></thead>
              <tbody>{data.highlights.map((h: string) => <tr key={h}><td className="flag" aria-hidden>!</td><td>{h}</td></tr>)}</tbody>
            </table>
          )}

          {groups.length === 0 && <p className="muted">Nothing was recorded on this day.</p>}
          {groups.map((g) => (
            <table key={g.label} className="report log" aria-label={g.label}>
              <thead><tr><th className="section" colSpan={2}>{g.label}</th></tr></thead>
              <tbody>{g.events.map((e: any, i: number) => (
                <tr key={i}>
                  <td className="time">{clock(e.at)}</td>
                  <td>{e.text}{e.by && !e.text.startsWith(e.by) && <span className="by">{e.by}</span>}</td>
                </tr>
              ))}</tbody>
            </table>
          ))}
        </section>
      )}
    </>
  );
}
