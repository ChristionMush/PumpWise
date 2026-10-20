import { type ChangeEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { api, ApiError, downloadFile, errorMessage } from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";

interface RowError { file: "meters" | "cash"; row: number | null; message: string }
interface Summary {
  saved: boolean; shifts: number; readings: number; cash_rows: number; from: string; to: string; stations: string[];
  litres: string; amount_by_currency: Record<string, string>; flagged_shifts: number;
  prices_added: { station: string; fuel: string; currency: string; price_per_litre: string; from: string; to: string }[];
}

const day = (iso: string) => new Date(iso).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });

/** Owner: bring in past shifts from old books or spreadsheets. Checked first; nothing is saved until "Import". */
export default function Import() {
  const { user } = useAuth();
  const [meters, setMeters] = useState<File | null>(null);
  const [cash, setCash] = useState<File | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rowErrors, setRowErrors] = useState<RowError[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [inputs, setInputs] = useState(0); // bumped to clear the file pickers after an import
  if (user?.role !== "owner") return <Navigate to="/" replace />;

  const pick = (set: (f: File | null) => void) => (e: ChangeEvent<HTMLInputElement>) => {
    set(e.target.files?.[0] ?? null); setSummary(null); setRowErrors([]); setError(null); // a new file needs a new check
  };
  const send = async (commit: boolean) => {
    if (!meters) return setError("Choose the meters file.");
    const form = new FormData();
    form.append("meters", meters);
    if (cash) form.append("cash", cash);
    form.append("commit", String(commit));
    setBusy(true); setError(null); setRowErrors([]);
    try {
      const result = await api<Summary>("/api/shifts/import/", { method: "POST", body: form });
      setSummary(result);
      if (result.saved) { setMeters(null); setCash(null); setInputs(inputs + 1); }
    } catch (e) {
      setSummary(null);
      if (e instanceof ApiError && Array.isArray(e.data?.errors)) setRowErrors(e.data.errors);
      else setError(errorMessage(e));
    } finally { setBusy(false); }
  };
  const template = (file: string) => downloadFile(`/api/shifts/import/template/?file=${file}`, `${file}-template.csv`).catch((e) => setError(errorMessage(e)));

  return (
    <>
      <h1>Import old shifts</h1>
      <p className="muted">Bring in shifts from your old books or spreadsheets. Fill in the templates in Excel and save them as CSV. Nothing is saved until the files are checked and you press Import.</p>
      <ErrorNote message={error} />

      <section className="card form"><h2>1. Fill in the templates</h2>
        <div className="row"><button className="btn" onClick={() => void template("meters")}>Meters template</button><button className="btn" onClick={() => void template("cash")}>Cash template</button></div>
        <ul className="muted">
          <li>Meters: one row per nozzle per shift. Your stations, pumps and nozzles are already listed; copy rows for more shifts.</li>
          <li>Dates day first (31/03/2026). A night shift ending at 06:00 ends the next morning.</li>
          <li>Several attendants: separate names (or phone numbers) with ";". Former staff must still be listed under Settings (switched off is fine).</li>
          <li>Price per litre is needed only for dates before your price history starts.</li>
          <li>Cash (optional): one row per attendant per payment type: cash, ecocash, card or credit.</li>
        </ul>
      </section>

      <section className="card form" key={inputs}><h2>2. Check your files</h2>
        <label>Meters file<input type="file" accept=".csv,text/csv" onChange={pick(setMeters)} /></label>
        <label>Cash file (optional)<input type="file" accept=".csv,text/csv" onChange={pick(setCash)} /></label>
        <button className="btn primary" disabled={busy || !meters} onClick={() => void send(false)}>{busy && !summary ? "Checking…" : "Check files"}</button>
      </section>

      {rowErrors.length > 0 && (
        <section className="card bad" role="alert"><h2>Fix these rows, then check again</h2>
          <ul className="plain">{rowErrors.map((e, i) => <li key={i}><strong>{e.file === "cash" ? "Cash" : "Meters"}{e.row ? ` row ${e.row}` : ""}:</strong> {e.message}</li>)}</ul>
        </section>
      )}

      {summary && (
        <section className="card" role="status"><h2>{summary.saved ? `Imported ${summary.shifts} shift${summary.shifts === 1 ? "" : "s"}` : "Ready to import"}</h2>
          <p>{summary.shifts} shift{summary.shifts === 1 ? "" : "s"} at {summary.stations.join(", ")}, {day(summary.from)} to {day(summary.to)}</p>
          <p>{Number(summary.litres).toLocaleString()} L sold · {Object.entries(summary.amount_by_currency).map(([c, a]) => `${c} ${Number(a).toLocaleString(undefined, { minimumFractionDigits: 2 })}`).join(" · ") || "no sales"} · {summary.cash_rows} cash amounts</p>
          {summary.prices_added.length > 0 && (<><p>Old prices added to price history:</p>
            <ul className="muted">{summary.prices_added.map((p, i) => <li key={i}>{p.fuel} at {p.station}: {p.currency} {Number(p.price_per_litre).toFixed(2)}, {day(p.from)} to {day(p.to)}</li>)}</ul></>)}
          {summary.flagged_shifts > 0 && <p className="warn">{summary.flagged_shifts} shift{summary.flagged_shifts === 1 ? " has" : "s have"} a note to look at (for example a meter gap). You will find them in Reports.</p>}
          {!summary.saved && <button className="btn primary big" disabled={busy} onClick={() => void send(true)}>{busy ? "Importing…" : `Import ${summary.shifts} shift${summary.shifts === 1 ? "" : "s"}`}</button>}
        </section>
      )}
    </>
  );
}
