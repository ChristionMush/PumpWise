import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, tokens } from "../api";
import { useAuth } from "../auth";
import { ErrorNote } from "../components";
import { t } from "../i18n";

export default function ChangePassword() {
  const { user, reload } = useAuth();
  const navigate = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ access: string; refresh: string }>("/api/auth/change-password/", {
        method: "POST",
        body: { current_password: current, new_password: next },
      });
      tokens.set(res.access, res.refresh); // old tokens were revoked; this device stays signed in
      await reload();
      navigate("/", { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="card form">
      <h1>{t("pw.title")}</h1>
      {user?.must_change_password && <p>{t("pw.intro")}</p>}
      <label>{t("pw.current")}
        <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />
      </label>
      <label>{t("pw.new")}
        <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" minLength={8} required />
      </label>
      <ErrorNote message={error} />
      <button className="btn primary" disabled={busy}>{t("pw.submit")}</button>
    </form>
  );
}
