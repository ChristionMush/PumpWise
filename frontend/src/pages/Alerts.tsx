import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";

export default function Alerts() {
  const [rows, setRows] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = () => api("/api/alerts/").then((d) => setRows(d.results)).catch((e) => setError(errorMessage(e)));
  useEffect(() => { void load(); }, []);
  const resolve = async (id: string) => { await api(`/api/alerts/${id}/resolve/`, { method: "POST" }); void load(); };
  const refresh = async () => { await api("/api/alerts/refresh/", { method: "POST" }); void load(); };
  return (
    <>
      <div className="row between"><h1>Needs attention</h1><button className="btn" onClick={() => void refresh()}>Check now</button></div>
      <ErrorNote message={error} />
      {rows.length === 0 && <p className="muted center">All good. Nothing needs your attention.</p>}
      {rows.map((a) => (
        <section key={a.id} className={`card ${a.severity === "critical" ? "bad" : ""}`}>
          <div className="row between"><Badge kind={a.severity === "info" ? "active" : "suspended"}>{a.kind.replace("_", " ")}</Badge><span className="muted">{new Date(a.created_at).toLocaleDateString()}</span></div>
          <p>{a.message}</p>
          {["loss", "cash"].includes(a.kind) && <button className="btn" onClick={() => void resolve(a.id)}>Mark as checked</button>}
        </section>
      ))}
    </>
  );
}
