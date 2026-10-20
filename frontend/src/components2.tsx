import { useEffect, useState } from "react";
import { api } from "./api";
import { useAuth } from "./auth";
import { getSyncState, onSyncState, syncNow, type SyncState } from "./offline";

export function SyncBadge() {
  const [s, setS] = useState<SyncState>(getSyncState());
  useEffect(() => onSyncState(setS), []);
  const text = !s.online ? `Offline${s.pending ? ` · ${s.pending} saved` : ""}` : s.syncing ? "Syncing…" : s.pending ? `${s.pending} waiting` : "All saved";
  return <button className={`sync ${!s.online ? "off" : s.pending ? "wait" : "ok"}`} onClick={() => void syncNow()} aria-label="Sync status">{text}</button>;
}

const STEPS = [
  ["Start your shift", "Tap Shift, check the meter numbers on each pump, and press Start. The last numbers are already filled in."],
  ["End your shift", "Enter the new meter numbers and the money you hand in. The app shows if it adds up."],
  ["No signal? Keep going", "Everything is saved on your phone and sent when the signal comes back. Look for “All saved” at the top."],
];
export function Tutorial({ onDone }: { onDone: () => void }) {
  const [i, setI] = useState(0);
  return (
    <div className="overlay" role="dialog" aria-label="How it works"><div className="card">
      <h2>{STEPS[i][0]}</h2><p>{STEPS[i][1]}</p>
      <div className="row between"><span className="muted">{i + 1} / {STEPS.length}</span>
        <button className="btn primary" onClick={() => (i + 1 < STEPS.length ? setI(i + 1) : onDone())}>{i + 1 < STEPS.length ? "Next" : "Got it"}</button></div>
    </div></div>
  );
}

interface PriceNote {
  id: string; station_name: string; fuel: string; currency: string; price_per_litre: string; previous_price: string | null;
  effective_from: string; upcoming: boolean; set_by?: string | null; set_by_role?: string | null; set_at?: string;
}
const at = (iso: string) => new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** New fuel prices on everyone's home screen, so nobody sells at an old price. The server adds who set each one (and
 *  when) for the owner only. The last list stays on the phone, per person, for when there is no signal. */
export function PriceNews() {
  const { user } = useAuth();
  const key = `fs_price_news:${user?.id}`;
  const [items, setItems] = useState<PriceNote[]>(() => { try { return JSON.parse(localStorage.getItem(key) || "[]"); } catch { return []; } });
  useEffect(() => {
    api<{ announcements: PriceNote[] }>("/api/prices/announcements/").then((r) => {
      setItems(r.announcements);
      try { localStorage.setItem(key, JSON.stringify(r.announcements)); } catch { /* storage blocked: fine */ }
    }).catch(() => undefined); // offline: keep the saved list
  }, [key]);
  if (!items.length) return null;
  return (
    <section className="card price-news" aria-label="Price changes"><h2>Price changes</h2>
      <ul className="plain">{items.map((p) => (
        <li key={p.id}>
          <strong>{p.fuel}: {p.currency} {Number(p.price_per_litre).toFixed(2)}</strong>
          {p.previous_price && <span className="muted"> (was {Number(p.previous_price).toFixed(2)})</span>}
          <span> at {p.station_name}, {p.upcoming ? "from" : "since"} {at(p.effective_from)}</span>
          {p.set_at && <span className="muted setter">Set by {p.set_by ?? "setup"}{p.set_by_role ? ` (${p.set_by_role})` : ""} on {at(p.set_at)}</span>}
        </li>))}</ul>
    </section>
  );
}
