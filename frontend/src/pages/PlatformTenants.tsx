import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api";
import { Badge, ErrorNote } from "../components";
import { t } from "../i18n";
import type { Page, TenantSummary } from "../types";

const FILTERS = [["", "All"], ["active", "Active"], ["suspended", "Suspended"], ["cancelled", "Cancelled"]] as const;
const LIMIT = 25;

export default function PlatformTenants() {
  const [rows, setRows] = useState<TenantSummary[]>([]);
  const [more, setMore] = useState(false);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (offset: number) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
      if (status) params.set("status", status);
      if (q.trim()) params.set("q", q.trim());
      const page = await api<Page<TenantSummary>>(`/api/platform/tenants/?${params}`);
      setRows((prev) => (offset === 0 ? page.results : [...prev, ...page.results]));
      setMore(!!page.next);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [status, q]);

  useEffect(() => {
    const id = setTimeout(() => void load(0), q ? 300 : 0); // small debounce while typing
    return () => clearTimeout(id);
  }, [load, q]);

  return (
    <>
      <div className="row between">
        <h1>{t("platform.title")}</h1>
        <Link to="/platform/new" className="btn primary">{t("platform.new")}</Link>
      </div>
      <input type="search" placeholder={t("platform.search")} value={q} onChange={(e) => setQ(e.target.value)} aria-label={t("platform.search")} />
      <div className="chips" role="group" aria-label="Filter by status">
        {FILTERS.map(([value, label]) => (
          <button key={value} className={`chip ${status === value ? "on" : ""}`} aria-pressed={status === value} onClick={() => setStatus(value)}>{label}</button>
        ))}
      </div>
      <ErrorNote message={error} />
      {rows.map((r) => (
        <Link key={r.id} to={`/platform/tenants/${r.id}`} className="card tenant">
          <div className="row between">
            <strong>{r.name}</strong>
            <Badge kind={r.status}>{r.status}</Badge>
          </div>
          <div className="row gap muted">
            {r.subscription && <Badge kind={r.subscription.status}>{`sub: ${r.subscription.status}`}</Badge>}
            <span>{r.station_count} {r.station_count === 1 ? "station" : "stations"}</span>
            {r.owner && <span>{r.owner.full_name}</span>}
          </div>
        </Link>
      ))}
      {!loading && !error && rows.length === 0 && <p className="muted center">{t("platform.empty")}</p>}
      {loading && <p className="muted center">{t("common.loading")}</p>}
      {more && !loading && <button className="btn" onClick={() => void load(rows.length)}>{t("platform.more")}</button>}
    </>
  );
}
