import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge } from "../components";
import { t } from "../i18n";
import Dashboard from "./Dashboard";
import Shift from "./Shift";
import type { Subscription } from "../types";

interface Sub extends Subscription { tenant_status: string }

/** What a suspended owner/manager sees. Payment itself arrives in Phase 8. */
function Locked() {
  const [sub, setSub] = useState<Sub | null>(null);
  useEffect(() => { api<Sub>("/api/subscription/").then(setSub).catch(() => setSub(null)); }, []);
  return (
    <section className="card lock" role="alert">
      <h1>{t("lock.title")}</h1>
      <p>{t("lock.body")}</p>
      {sub && (
        <dl>
          <dt>Subscription</dt><dd><Badge kind={sub.status}>{sub.status}</Badge></dd>
          {sub.plan && (<><dt>Plan</dt><dd>{sub.plan}</dd></>)}
          {sub.paid_through && (<><dt>Paid through</dt><dd>{sub.paid_through}</dd></>)}
        </dl>
      )}
    </section>
  );
}

export default function Home() {
  const { user, tenant } = useAuth();
  const locked =
    tenant && (tenant.status === "cancelled" || (tenant.status === "suspended" && (user?.role === "owner" || user?.role === "manager")));
  if (locked) return <Locked />;
  return user?.role === "attendant" ? <Shift /> : <Dashboard />;
}
