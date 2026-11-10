import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";
import { enqueue, uuid } from "../offline";
import { loadPrefill, type StationInfo } from "./Shift";

interface ExpenseRow {
  id: string; station: string; station_name: string; category: string; category_label: string; amount: string; currency: string;
  note: string; spent_at: string; recorded_by_name: string;
}

const CATEGORIES = [["lunch", "Lunch"], ["generator_fuel", "Generator fuel"], ["transport", "Transport"], ["repairs", "Repairs and maintenance"], ["other", "Other"]] as const;
const when = (iso: string) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/**
 * Expenses (a business the platform switched them on for): money paid out at a station, such as lunch or generator
 * fuel. Recorded only: the cash an attendant hands in is unchanged. Saved on the phone first, like everything else.
 * Attendants see their own; managers (their stations) and the owner see every expense, also in the accounting pack.
 */
export default function Expenses() {
  const { user } = useAuth();
  const [stations, setStations] = useState<StationInfo[]>([]);
  const [form, setForm] = useState({ station: "", category: "lunch", amount: "", note: "" });
  const [rows, setRows] = useState<ExpenseRow[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadRows = () => api<ExpenseRow[]>("/api/expenses/").then(setRows).catch(() => setRows(null)); // no signal: the list waits
  useEffect(() => {
    void loadPrefill().then((s) => { setStations(s); setForm((f) => ({ ...f, station: f.station || s[0]?.id || "" })); });
    void loadRows();
  }, []);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const amount = Number(form.amount);
    if (!form.station) return setError("Choose the station.");
    if (!(amount > 0)) return setError("Enter what was paid.");
    if (form.category === "other" && !form.note.trim()) return setError("Say what the money was paid for.");
    await enqueue("expense", { client_id: uuid(), station: form.station, category: form.category, amount: form.amount, note: form.note.trim(), spent_at: new Date().toISOString() });
    setForm({ ...form, amount: "", note: "" }); setError(null);
    setMsg("Expense saved. It does not change the cash you hand in.");
    setTimeout(() => void loadRows(), 1500); // after the phone has sent it
  };

  const own = user?.role === "attendant";
  const total = (rows ?? []).reduce<Record<string, number>>((t, r) => ({ ...t, [r.currency]: (t[r.currency] ?? 0) + Number(r.amount) }), {});
  return (
    <>
      <h1>Expenses</h1>
      <form onSubmit={save} className="card form" aria-label="Record an expense">
        <h2>Record an expense</h2>
        {stations.length > 1 && <label>Station<select value={form.station} onChange={(e) => setForm({ ...form, station: e.target.value })}>{stations.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>}
        <label>Paid for<select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>{CATEGORIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label>Amount{stations.find((s) => s.id === form.station) ? ` (${stations.find((s) => s.id === form.station)!.pricing_currency})` : ""}
          <input inputMode="decimal" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value.replace(",", ".") })} /></label>
        <label>{form.category === "other" ? "What was it for" : "Note (optional)"}<input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="e.g. 20 L diesel for the generator" /></label>
        <button className="btn primary">Save expense</button>
        <p className="muted">Recorded only: it does not change the cash you hand in.</p>
      </form>
      <ErrorNote message={error} />{msg && <p role="status" className="ok-note">{msg}</p>}
      <section className="card">
        <h2>{own ? "My expenses" : "Expenses recorded"}</h2>
        {rows === null ? <p className="muted">The list shows when there is signal.</p> : rows.length === 0 ? <p className="muted">No expenses yet.</p> : (
          <>
            <table className="table" aria-label="Expenses">
              <thead><tr><th>When</th>{!own && <th>Paid by</th>}<th>For</th><th>Note</th><th>Amount</th></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.id}><td>{when(r.spent_at)}{stations.length > 1 ? ` · ${r.station_name}` : ""}</td>{!own && <td>{r.recorded_by_name}</td>}
                  <td>{r.category_label}</td><td>{r.note}</td><td>{r.currency} {Number(r.amount).toFixed(2)}</td></tr>
              ))}</tbody>
            </table>
            <p><strong>Total: {Object.entries(total).map(([c, v]) => `${c} ${v.toFixed(2)}`).join(" · ")}</strong></p>
          </>
        )}
      </section>
    </>
  );
}
