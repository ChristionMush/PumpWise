import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { ErrorNote } from "../components";
import type { MarkInfo, ModuleInfo, Modules } from "../types";
import ModulePicker, { changedModules, useCatalog } from "./ModulePicker";

/** Platform: the Marks, each with its modules and the businesses on it. Duplicate one to make a new Mark from it. */
export default function PlatformMarks() {
  const { modules, marks, error, reload } = useCatalog();
  return (
    <>
      <div className="row between"><h1>Marks</h1><Link to="/platform" className="btn">Businesses</Link></div>
      <p className="muted">A Mark is a way of running PumpWise: the modules a new business starts with. Changing a Mark never changes a
        business already on it; each business keeps its own modules. To make a new way of working, duplicate the closest Mark and change the copy.</p>
      <ErrorNote message={error} />
      {modules && marks?.map((k) => <MarkCard key={k.id} mark={k} modules={modules} marks={marks} reload={reload} />)}
    </>
  );
}

function MarkCard({ mark, modules, marks, reload }: { mark: MarkInfo; modules: ModuleInfo[]; marks: MarkInfo[]; reload: () => Promise<void> }) {
  const [draft, setDraft] = useState<Modules>(mark.modules);
  const [name, setName] = useState(mark.name);
  const [description, setDescription] = useState(mark.description);
  const [copyName, setCopyName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const changed = changedModules(modules, mark.modules, draft);
  const edited = changed.length > 0 || name.trim() !== mark.name || description.trim() !== mark.description;

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setError(null); setNote(null);
    try { await fn(); await reload(); setNote(ok); return true; } catch (e) { setError(errorMessage(e)); return false; }
  };
  const save = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api(`/api/platform/marks/${mark.id}/`, { method: "PATCH", body: {
      name: name.trim(), description: description.trim(), modules: Object.fromEntries(changed.map((m) => [m.key, draft[m.key]])),
    } }), `${name.trim()} saved. Businesses on it keep their own modules.`);
  };
  const duplicate = (e: FormEvent) => {
    e.preventDefault();
    void run(() => api("/api/platform/marks/", { method: "POST", body: { duplicate_of: mark.id, name: copyName.trim() } }), `${copyName.trim()} made from ${mark.name}.`)
      .then((ok) => { if (ok) setCopyName(""); });
  };

  return (
    <section className="card form" aria-label={mark.name}>
      <h2>{mark.name}</h2>
      {mark.based_on && <p className="muted">{`Made from ${mark.based_on.name}.`}</p>}
      <p>{mark.description}</p>
      <p className="muted">{mark.tenants?.length ? `Businesses on it: ${mark.tenants.map((t) => t.name).join(", ")}.` : "No business on it yet."}</p>
      <details>
        <summary>Change {mark.name}</summary>
        <form onSubmit={save} className="form">
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} required /></label>
          <label>What it is for<textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} /></label>
          <ModulePicker modules={modules} marks={marks} values={draft} onChange={setDraft} />
          <button className="btn primary" disabled={!edited}>Save {mark.name}</button>
        </form>
      </details>
      <form onSubmit={duplicate} className="form" aria-label={`Duplicate ${mark.name}`}>
        <label>New Mark from {mark.name}<input value={copyName} onChange={(e) => setCopyName(e.target.value)} placeholder="e.g. Mark 3" required /></label>
        <button className="btn">Duplicate</button>
      </form>
      <ErrorNote message={error} />{note && <p role="status" className="ok-note">{note}</p>}
    </section>
  );
}
