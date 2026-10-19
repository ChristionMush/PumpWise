import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Footer } from "./components";
import { SyncBadge, Tutorial } from "./components2";
import { startAutoSync } from "./offline";
import Alerts from "./pages/Alerts";
import Dashboard from "./pages/Dashboard";
import Dip from "./pages/Dip";
import DipHistory from "./pages/DipHistory";
import Prices from "./pages/Prices";
import Pumps from "./pages/Pumps";
import Reports from "./pages/Reports";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";
import Shift from "./pages/Shift";
import { t } from "./i18n";
import ChangePassword from "./pages/ChangePassword";
import CreateTenant from "./pages/CreateTenant";
import Home from "./pages/Home";
import Login from "./pages/Login";
import PlatformTenants from "./pages/PlatformTenants";
import TenantDetail from "./pages/TenantDetail";

// Attendants record meters and their own cash only. Dips and deliveries (PLAN.md rule 11) and assigning pumps are for
// managers and owners; the server refuses attendants anyway, this just keeps the screens out of their way.
const NAV: Record<string, [string, string][]> = {
  owner: [["/", "Today"], ["/dip", "Dip & delivery"], ["/dip-history", "Dip history"], ["/reports", "Reports"], ["/alerts", "Alerts"], ["/prices", "Prices"], ["/pumps", "Pumps"], ["/setup", "Setup"], ["/settings", "Settings"]],
  manager: [["/", "Today"], ["/dip", "Dip & delivery"], ["/dip-history", "Dip history"], ["/reports", "Reports"], ["/alerts", "Alerts"], ["/prices", "Prices"], ["/pumps", "Pumps"], ["/setup", "Setup"]],
  attendant: [["/", "Shift"]],
};

function Shell({ children }: { children: ReactNode }) {
  const { user, tenant, branding, billing, logout } = useAuth();
  const [tutorial, setTutorial] = useState(() => user?.role === "attendant" && !localStorage.getItem("fs_tutorial_done"));
  useEffect(() => (user && user.role !== "platform_admin" ? startAutoSync() : undefined), [user]);
  const name = branding?.display_name || tenant?.name || t("appName");
  const nav = user ? NAV[user.role] : undefined;
  return (
    <div className="app">
      <header className="header">
        <img className="logo" src={branding?.logo_url || "/icon.svg"} alt="" />
        <Link to="/" className="brand">{name}</Link>
        {user && user.role !== "platform_admin" && <SyncBadge />}
        <span className="who">{user?.full_name}</span>
        <button className="link" onClick={() => void logout()}>{t("common.signOut")}</button>
      </header>
      {tenant?.is_demo && <div className="banner demo" role="status">Demo data{tenant.trial_expires_at ? ` · trial until ${tenant.trial_expires_at}` : ""}. Nothing here is a real business.</div>}
      {billing && billing.level !== "info" && <div className={`banner ${billing.level}`} role="status">{billing.message}</div>}
      {nav && <nav className="nav" aria-label="Main">{nav.map(([to, label]) => <NavLink key={to} to={to} end={to === "/"}>{label}</NavLink>)}</nav>}
      <main className="main">{children}</main>
      <Footer />
      {tutorial && <Tutorial onDone={() => { localStorage.setItem("fs_tutorial_done", "1"); setTutorial(false); }} />}
    </div>
  );
}

function Private({ children, platform = false, managers = false }: { children: ReactNode; platform?: boolean; managers?: boolean }) {
  const { user, loading } = useAuth();
  const { pathname } = useLocation();
  if (loading) return <p className="center muted">{t("common.loading")}</p>;
  if (!user) return <Navigate to="/login" replace />;
  if (user.must_change_password && pathname !== "/change-password") return <Navigate to="/change-password" replace />;
  if (platform && user.role !== "platform_admin") return <Navigate to="/" replace />;
  if (managers && user.role !== "owner" && user.role !== "manager") return <Navigate to="/" replace />;
  return <Shell>{children}</Shell>;
}

export default function App() {
  const { user } = useAuth();
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/change-password" element={<Private><ChangePassword /></Private>} />
      <Route path="/dashboard" element={<Private><Dashboard /></Private>} />
      <Route path="/shift" element={<Private><Shift /></Private>} />
      <Route path="/dip" element={<Private managers><Dip /></Private>} />
      <Route path="/dip-history" element={<Private managers><DipHistory /></Private>} />
      <Route path="/pumps" element={<Private managers><Pumps /></Private>} />
      <Route path="/reports" element={<Private><Reports /></Private>} />
      <Route path="/alerts" element={<Private><Alerts /></Private>} />
      <Route path="/prices" element={<Private><Prices /></Private>} />
      <Route path="/setup" element={<Private><Setup /></Private>} />
      <Route path="/settings" element={<Private><Settings /></Private>} />
      <Route path="/platform" element={<Private platform><PlatformTenants /></Private>} />
      <Route path="/platform/new" element={<Private platform><CreateTenant /></Private>} />
      <Route path="/platform/tenants/:id" element={<Private platform><TenantDetail /></Private>} />
      <Route
        path="/"
        element={user?.role === "platform_admin" ? <Navigate to="/platform" replace /> : <Private><Home /></Private>}
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
