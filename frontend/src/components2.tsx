import { useEffect, useState } from "react";
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
