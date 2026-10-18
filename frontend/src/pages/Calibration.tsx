import { useState, type ChangeEvent } from "react";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";

export interface ChartRow { dip_mm: number; litres: number }

/** Reads a calibration chart typed, pasted or saved as CSV: one row per line, dip height in mm then litres.
 *  Separators may be a comma, semicolon, tab or spaces. A first line of words (a header) is skipped. */
export function parseChart(text: string): { rows: ChartRow[]; errors: string[] } {
  const rows: ChartRow[] = [];
  const errors: string[] = [];
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const cells = line.split(/[\t;]|,\s*|\s+/).filter(Boolean);
    const nums = cells.map(Number);
    if (cells.length === 2 && nums.every((n) => Number.isFinite(n))) {
      rows.push({ dip_mm: nums[0], litres: nums[1] });
    } else if (rows.length === 0 && nums.some((n) => Number.isNaN(n)) && !errors.length) {
      // a header such as "Dip (mm), Litres": skipped
    } else {
      errors.push(`Line ${i + 1}: expected two numbers (dip height in mm, then litres), found "${line}".`);
    }
  });
  const sorted = [...rows].sort((a, b) => a.dip_mm - b.dip_mm);
  if (rows.length && rows.length < 2) errors.push("A chart needs at least two rows.");
  if (new Set(rows.map((r) => r.dip_mm)).size !== rows.length) errors.push("Two rows have the same dip height.");
  if (sorted.some((r, i) => i > 0 && r.litres < sorted[i - 1].litres)) errors.push("Litres must go up as the dip height goes up.");
  return { rows: sorted, errors };
}

/** Owners and managers: load a tank's calibration chart (dip height in mm to litres) so dips can be taken in mm. */
export function CalibrationEditor({ tank, onSaved, onClose }: { tank: any; onSaved: () => void; onClose: () => void }) {
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { rows, errors } = parseChart(text);

  const readFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (/\.xlsx?$/i.test(f.name)) return setError("That is an Excel file. In Excel choose File, Save As, \"CSV (Comma delimited)\", then upload the CSV.");
    setError(null); setFileName(f.name); setText(await f.text());
  };
  const loadCurrent = async () => {
    try {
      const points = await api<ChartRow[]>(`/api/tanks/${tank.id}/calibration/`);
      setText(points.map((p) => `${Number(p.dip_mm)}, ${Number(p.litres)}`).join("\n")); setFileName(null);
    } catch (e) { setError(errorMessage(e)); }
  };
  const save = async () => {
    setBusy(true); setError(null);
    try {
      await api(`/api/tanks/${tank.id}/calibration/`, { method: "PUT", body: { points: rows } });
      onSaved();
    } catch (e) { setError(errorMessage(e)); } finally { setBusy(false); }
  };

  return (
    <div className="calib" role="group" aria-label={`Calibration chart for ${tank.name}`}>
      <p>Each row is a dip height in millimetres and the litres in the tank at that height, from the tank supplier's chart. Example:</p>
      <pre className="sample">Dip (mm), Litres{"\n"}0, 0{"\n"}400, 6000{"\n"}800, 13500{"\n"}1200, 20000</pre>
      <label>Upload a chart file (CSV or text)
        <input type="file" accept=".csv,.txt,text/csv,text/plain" onChange={(e) => void readFile(e)} />
      </label>
      <label>Or type or paste the rows
        <textarea rows={6} value={text} onChange={(e) => { setText(e.target.value); setFileName(null); }} placeholder={"0, 0\n400, 6000\n800, 13500"} />
      </label>
      {tank.has_calibration && !text && <button type="button" className="link" onClick={() => void loadCurrent()}>Load the current chart to change it</button>}
      {fileName && <p className="muted">Read from {fileName}.</p>}
      {errors.map((m) => <p key={m} className="warn">{m}</p>)}
      {rows.length > 0 && !errors.length && (
        <div className="table-scroll"><table className="table">
          <caption className="muted">{rows.length} rows, {rows[0].dip_mm} to {rows[rows.length - 1].dip_mm} mm, up to {rows[rows.length - 1].litres.toLocaleString()} L</caption>
          <thead><tr><th>Dip (mm)</th><th>Litres</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={r.dip_mm}><td>{r.dip_mm}</td><td>{r.litres.toLocaleString()}</td></tr>)}</tbody>
        </table></div>
      )}
      <ErrorNote message={error} />
      <div className="row gap">
        <button type="button" className="btn primary" disabled={busy || rows.length < 2 || errors.length > 0} onClick={() => void save()}>{busy ? "Saving…" : "Save chart"}</button>
        <button type="button" className="btn" onClick={onClose}>Cancel</button>
      </div>
    </div>
  );
}
