import { useEffect, useState } from "react";
import { api, errorMessage } from "../api";
import type { MarkInfo, ModuleInfo, Modules } from "../types";

/** The modules and the Marks, as the platform screens tick them. */
export function useCatalog() {
  const [modules, setModules] = useState<ModuleInfo[] | null>(null);
  const [marks, setMarks] = useState<MarkInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = () => Promise.all([api<{ modules: ModuleInfo[] }>("/api/platform/modules/"), api<{ marks: MarkInfo[] }>("/api/platform/marks/")])
    .then(([m, k]) => { setModules(m.modules); setMarks(k.marks); })
    .catch((e) => setError(errorMessage(e)));
  useEffect(() => { void reload(); }, []);
  return { modules, marks, error, reload };
}

/** Ticking a module ticks what it needs; unticking one unticks what needs it. */
export function withModule(modules: ModuleInfo[], values: Modules, key: keyof Modules, value: string | boolean): Modules {
  const next = { ...values, [key]: value } as Modules;
  const mod = modules.find((m) => m.key === key)!;
  if (value === true) for (const n of mod.needs) (next as any)[n] = true;
  if (value === false) for (const m of modules) if (m.needs.includes(key)) (next as any)[m.key] = false;
  return next;
}

/** The modules that differ between two sets, by name. */
export function changedModules(modules: ModuleInfo[], from: Modules, to: Modules) {
  return modules.filter((m) => from[m.key] !== to[m.key]);
}

const shown = (m: ModuleInfo, v: unknown) => (m.kind === "on_off" ? (v ? "on" : "off") : m.choices.find((c) => c.value === v)?.label ?? String(v));

interface Props {
  modules: ModuleInfo[];
  marks: MarkInfo[];
  values: Modules;
  onChange: (values: Modules) => void;
  /** The Mark the business starts from: its modules come first, the other Marks' ones under "Also available". */
  base?: MarkInfo | null;
  disabled?: boolean;
}

/** Tick a business's (or a Mark's) modules. Each one says what it does, what it needs, and which Marks use it. */
export default function ModulePicker({ modules, marks, values, onChange, base, disabled }: Props) {
  const others = marks.filter((k) => k.id !== base?.id);
  const usedBy = (key: keyof Modules, value: unknown) => others.filter((k) => k.modules[key] === value).map((k) => k.name);
  const inBase = base ? modules.filter((m) => m.kind === "choice" || base.modules[m.key] === true) : modules;
  const borrowed = base ? modules.filter((m) => !inBase.includes(m) && usedBy(m.key, true).length) : [];
  const rest = base ? modules.filter((m) => !inBase.includes(m) && !borrowed.includes(m)) : [];

  const row = (m: ModuleInfo) => {
    const changed = base && values[m.key] !== base.modules[m.key];
    const needs = m.needs.map((n) => modules.find((x) => x.key === n)?.label).filter(Boolean);
    return (
      <div key={m.key} className="form">
        {m.kind === "on_off" ? (
          <label className="row gap"><input type="checkbox" checked={Boolean(values[m.key])} disabled={disabled}
            onChange={(e) => onChange(withModule(modules, values, m.key, e.target.checked))} /> {m.label}</label>
        ) : (
          <label>{m.label}
            <select value={String(values[m.key])} disabled={disabled} onChange={(e) => onChange(withModule(modules, values, m.key, e.target.value))}>
              {m.choices.map((c) => {
                const marksUsing = marks.filter((k) => k.modules[m.key] === c.value).map((k) => k.name);
                return <option key={c.value} value={c.value}>{c.label}{marksUsing.length ? ` (${marksUsing.join(", ")})` : ""}</option>;
              })}
            </select>
          </label>
        )}
        <p className="muted">{m.help}{needs.length ? ` Needs: ${needs.join(", ")}.` : ""}
          {m.kind === "on_off" && !base && usedBy(m.key, true).length ? ` On in ${usedBy(m.key, true).join(", ")}.` : ""}
          {changed && <> <span className="badge badge-past_due">{`${base.name}: ${shown(m, base.modules[m.key])}`}</span></>}</p>
      </div>
    );
  };

  return (
    <div className="form">
      {base && <h3>{base.name}</h3>}
      {inBase.map(row)}
      {borrowed.length > 0 && (<><h3>Also available</h3><p className="muted">From other Marks. Tick one to plug it in.</p>
        {borrowed.map((m) => <div key={m.key}>{row(m)}<p className="muted">{`From ${usedBy(m.key, true).join(", ")}.`}</p></div>)}</>)}
      {rest.length > 0 && (<><h3>Other modules</h3>{rest.map(row)}</>)}
    </div>
  );
}
