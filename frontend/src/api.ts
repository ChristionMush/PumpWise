// Small fetch wrapper: bearer token, one automatic refresh on 401, friendly errors.

const ACCESS = "fs_access";
const REFRESH = "fs_refresh";

export const tokens = {
  get access() { return localStorage.getItem(ACCESS); },
  get refresh() { return localStorage.getItem(REFRESH); },
  set(access: string, refresh: string) { localStorage.setItem(ACCESS, access); localStorage.setItem(REFRESH, refresh); },
  clear() { localStorage.removeItem(ACCESS); localStorage.removeItem(REFRESH); },
};

export class ApiError extends Error {
  constructor(public status: number, public data: any) {
    super(typeof data?.detail === "string" ? data.detail : `HTTP ${status}`);
  }
  get code(): string | undefined { return this.data?.code; }
}

let refreshing: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  if (!tokens.refresh) return false;
  refreshing ??= (async () => {
    try {
      const res = await fetch("/api/auth/refresh/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh: tokens.refresh }),
      });
      if (!res.ok) { tokens.clear(); window.dispatchEvent(new Event("fs:signed-out")); return false; }
      const data = await res.json();
      tokens.set(data.access, data.refresh);
      return true;
    } catch {
      return false; // offline: keep the tokens, the caller will see a network error
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; auth?: boolean } = {}): Promise<T> {
  const { method = "GET", body, auth = true } = opts;
  const send = () =>
    fetch(path, {
      method,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(auth && tokens.access ? { Authorization: `Bearer ${tokens.access}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

  let res = await send();
  if (res.status === 401 && auth && tokens.refresh && (await refreshOnce())) res = await send();
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

/** Plain-language message for any error. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.data;
    if (typeof d?.detail === "string") return d.detail;
    if (d && typeof d === "object") {
      const first = Object.values(d)[0] as unknown;
      const text = Array.isArray(first) ? first[0] : first;
      if (typeof text === "string") return text;
      if (text && typeof text === "object") return errorMessage(new ApiError(e.status, text));
    }
    return "Something went wrong. Please try again.";
  }
  if (e instanceof TypeError) return "No connection. Check your signal and try again.";
  return "Something went wrong. Please try again.";
}
