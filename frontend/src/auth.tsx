import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, tokens } from "./api";
import type { TenantInfo, User } from "./types";

interface AuthState {
  user: User | null;
  tenant: TenantInfo | null;
  billing: { level: string; message: string; days_past_due: number } | null;
  branding: { built_by: string; display_name?: string | null; logo_url?: string | null } | null;
  loading: boolean;
  login: (identifier: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  reload: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);
export const useAuth = () => {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [tenant, setTenant] = useState<TenantInfo | null>(null);
  const [billing, setBilling] = useState<AuthState["billing"]>(null);
  const [branding, setBranding] = useState<AuthState["branding"]>(null);
  const [loading, setLoading] = useState(!!tokens.access);

  const reload = useCallback(async () => {
    try {
      const me = await api<{ user: User; tenant: TenantInfo | null; billing?: AuthState["billing"]; branding?: AuthState["branding"] }>("/api/auth/me/");
      setUser(me.user);
      setTenant(me.tenant);
      setBilling(me.billing ?? null);
      setBranding(me.branding ?? null);
    } catch {
      setUser(null);
      setTenant(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tokens.access) void reload();
    const out = () => { setUser(null); setTenant(null); };
    window.addEventListener("fs:signed-out", out);
    return () => window.removeEventListener("fs:signed-out", out);
  }, [reload]);

  const login = useCallback(async (identifier: string, password: string) => {
    const res = await api<{ access: string; refresh: string }>("/api/auth/login/", { method: "POST", body: { identifier, password }, auth: false });
    tokens.set(res.access, res.refresh);
    await reload();
  }, [reload]);

  const logout = useCallback(async () => {
    try { await api("/api/auth/logout/", { method: "POST" }); } catch { /* offline: still sign out locally */ }
    tokens.clear();
    setUser(null);
    setTenant(null);
  }, []);

  const value = useMemo(() => ({ user, tenant, billing, branding, loading, login, logout, reload }), [user, tenant, billing, branding, loading, login, logout, reload]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
