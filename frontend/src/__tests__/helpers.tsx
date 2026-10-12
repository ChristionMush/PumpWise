import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import App from "../App";
import { AuthProvider } from "../auth";

type Handler = (body: any, url: string) => { status?: number; body?: unknown };
export type Routes = Record<string, Handler | { status?: number; body?: unknown }>;

/** Fake backend: keys are "METHOD /path" (query string ignored). Records every call. */
export function mockApi(routes: Routes) {
  const calls: { method: string; path: string; body: any }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url.pathname, body });
    const route = routes[`${method} ${url.pathname}`];
    if (!route) return new Response(JSON.stringify({ detail: `unmocked ${method} ${url.pathname}` }), { status: 404 });
    const out = typeof route === "function" ? route(body, String(input)) : route;
    return new Response(out.status === 204 ? null : JSON.stringify(out.body ?? {}), { status: out.status ?? 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

export const platformUser = { id: "u1", role: "platform_admin", full_name: "Tendeka", email: "t@x.com", phone: null, must_change_password: false, tenant_id: null };
export const ownerUser = { id: "u2", role: "owner", full_name: "Rudo", email: null, phone: "+263772123456", must_change_password: false, tenant_id: "t1" };
export function signedIn() {
  localStorage.setItem("fs_access", "tok");
  localStorage.setItem("fs_refresh", "ref");
}
