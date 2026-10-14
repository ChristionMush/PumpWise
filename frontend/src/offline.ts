// Offline-first queue. Entries are saved on the phone first (IndexedDB), then synced when there is signal.
// Every entry carries a client-made op_id, so retrying is always safe (the server ignores repeats).
import { api, ApiError } from "./api";

export type OpKind = "shift_start" | "checkpoint" | "shift_end" | "shift_join" | "shift_leave" | "shift_cash" | "shift_close" | "pump_reassign" | "tank_dip" | "delivery";
export interface QueuedOp { op_id: string; kind: OpKind; payload: any; queued_at: number }
export type SyncState = { pending: number; syncing: boolean; online: boolean; lastError: string | null; rejected: number };

const DB = "fuelstation", OPS = "ops", KV = "kv";
let memory: { ops: QueuedOp[]; kv: Record<string, unknown> } = { ops: [], kv: {} }; // fallback when IndexedDB is unavailable
let inflight: Promise<void> | null = null;
const listeners = new Set<(s: SyncState) => void>();
let state: SyncState = { pending: 0, syncing: false, online: typeof navigator === "undefined" ? true : navigator.onLine, lastError: null, rejected: 0 };

let dbp: Promise<IDBDatabase | null> | null = null;
function open(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === "undefined") return Promise.resolve(null);
  dbp ??= new Promise((resolve) => {
    const req = indexedDB.open(DB, 1);
    req.onupgradeneeded = () => { req.result.createObjectStore(OPS, { keyPath: "op_id" }); req.result.createObjectStore(KV); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => resolve(null);
  });
  return dbp;
}
const wrap = <T,>(r: IDBRequest<T>) => new Promise<T>((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });

export async function kvGet<T>(key: string): Promise<T | undefined> {
  const db = await open();
  if (!db) return memory.kv[key] as T | undefined;
  return wrap(db.transaction(KV).objectStore(KV).get(key));
}
export async function kvSet(key: string, value: unknown) {
  const db = await open();
  if (!db) { memory.kv[key] = value; return; }
  await wrap(db.transaction(KV, "readwrite").objectStore(KV).put(value, key));
}
export async function allOps(): Promise<QueuedOp[]> {
  const db = await open();
  const ops = db ? await wrap<QueuedOp[]>(db.transaction(OPS).objectStore(OPS).getAll()) : memory.ops;
  return [...ops].sort((a, b) => a.queued_at - b.queued_at);
}
async function putOp(op: QueuedOp) {
  const db = await open();
  if (!db) { memory.ops.push(op); return; }
  await wrap(db.transaction(OPS, "readwrite").objectStore(OPS).put(op));
}
async function delOps(ids: string[]) {
  const db = await open();
  if (!db) { memory.ops = memory.ops.filter((o) => !ids.includes(o.op_id)); return; }
  const store = db.transaction(OPS, "readwrite").objectStore(OPS);
  await Promise.all(ids.map((id) => wrap(store.delete(id))));
}
/** Wipe local data and state (used by tests, and available for a "reset this phone" action). */
export async function resetOffline() {
  memory = { ops: [], kv: {} };
  inflight = null;
  state = { pending: 0, syncing: false, online: true, lastError: null, rejected: 0 };
  if (dbp) { (await dbp)?.close(); dbp = null; }
  if (typeof indexedDB !== "undefined") await new Promise((r) => { const q = indexedDB.deleteDatabase(DB); q.onsuccess = q.onerror = q.onblocked = () => r(null); });
}

function emit(patch: Partial<SyncState>) { state = { ...state, ...patch }; listeners.forEach((l) => l(state)); }
export const getSyncState = () => state;
export function onSyncState(fn: (s: SyncState) => void) { listeners.add(fn); fn(state); return () => { listeners.delete(fn); }; }
export const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => { const r = (Math.random() * 16) | 0; return (c === "x" ? r : (r & 3) | 8).toString(16); }));

/** Save locally first (instant, works with no signal), then try to sync in the background. */
export async function enqueue(kind: OpKind, payload: any): Promise<string> {
  const op: QueuedOp = { op_id: uuid(), kind, payload, queued_at: Date.now() };
  await putOp(op);
  emit({ pending: (await allOps()).length });
  void syncNow();
  return op.op_id;
}

/** One batch call per shift (rule 8): the leading run of queued entries that belong to the same shift, in order. */
export function nextBatch(ops: QueuedOp[], max = 50): QueuedOp[] {
  const key = (o: QueuedOp) => o.payload?.shift_id ?? null;
  const first = ops.length ? key(ops[0]) : null;
  const out: QueuedOp[] = [];
  for (const o of ops) {
    if (key(o) !== first || out.length >= max) break;
    out.push(o);
  }
  return out;
}

/** Send the queue in order. Applied or rejected entries leave the queue (rejections are kept on the server for a manager). */
export function syncNow(): Promise<void> {
  inflight ??= runSync().finally(() => { inflight = null; }); // callers join a sync already in progress and wait for it
  return inflight;
}
async function runSync(): Promise<void> {
  try {
    for (;;) {
      const ops = nextBatch(await allOps());
      if (!ops.length) break;
      emit({ syncing: true, lastError: null });
      let res: { results: { op_id: string; status: string; error?: string }[] };
      try {
        res = await api("/api/sync/", { method: "POST", body: { ops: ops.map(({ op_id, kind, payload }) => ({ op_id, kind, payload })) } });
      } catch (e) {
        if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 401) emit({ lastError: "Some entries need a manager's help." });
        else emit({ lastError: "Waiting for signal." });
        break; // keep everything; try again later
      }
      const done = res.results.filter((r) => r.op_id).map((r) => r.op_id);
      await delOps(done.length ? done : ops.map((o) => o.op_id));
      emit({ rejected: state.rejected + res.results.filter((r) => r.status === "rejected").length });
    }
  } finally {
    emit({ syncing: false, pending: (await allOps()).length });
  }
}

export function startAutoSync() {
  const on = () => { emit({ online: true }); void syncNow(); };
  window.addEventListener("online", on);
  window.addEventListener("offline", () => emit({ online: false }));
  const timer = setInterval(() => { if (navigator.onLine) void syncNow(); }, 30000);
  void allOps().then((o) => { emit({ pending: o.length }); if (o.length) void syncNow(); });
  return () => { window.removeEventListener("online", on); clearInterval(timer); };
}

// ---- local calculation: expected vs handed in, instantly and offline (the server recalculates authoritatively) ----
export interface PriceRow { fuel_type: string; currency: string; price: string; from: string; to: string | null }
export function priceAt(prices: PriceRow[], fuelType: string, currency: string, at: Date): number | null {
  const hit = prices.filter((p) => p.fuel_type === fuelType && p.currency === currency && new Date(p.from) <= at && (!p.to || new Date(p.to) > at)).sort((a, b) => +new Date(b.from) - +new Date(a.from))[0];
  return hit ? Number(hit.price) : null;
}
export function litres(start: number, end: number, meterMax: number): number {
  if (end >= start) return end - start;
  return start > meterMax * 0.9 && end < meterMax * 0.1 ? meterMax - start + end + 0.01 : 0; // rollover, else flagged server-side
}
export function expectedVsHanded(rows: { fuel_type: string; start: number; end: number; meter_max: number }[], prices: PriceRow[], currency: string, startedAt: Date, handed: { amount: number; rate: number }[]) {
  const expected = rows.reduce((sum, r) => sum + litres(r.start, r.end, r.meter_max) * (priceAt(prices, r.fuel_type, currency, startedAt) ?? 0), 0);
  const submitted = handed.reduce((sum, h) => sum + h.amount * h.rate, 0);
  return { expected, submitted, variance: submitted - expected };
}
