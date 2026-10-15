import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { ApiError, errorMessage } from "../api";
import { useAuth } from "../auth";
import { ErrorNote, Footer, PasswordInput } from "../components";
import { t } from "../i18n";

export default function Login() {
  const { user, login } = useAuth();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to="/" replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(identifier.trim(), password);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? t("login.wrong") : errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app narrow">
      <main className="main center-col">
        <h1 aria-label="PumpWise"><img src="/icon.svg" className="login-logo" alt="PumpWise logo" width="200" height="200" /></h1>
        <form onSubmit={submit} className="card form">
          <h2>{t("login.title")}</h2>
          <label>{t("login.identifier")}
            <input value={identifier} onChange={(e) => setIdentifier(e.target.value)} autoComplete="username" inputMode="email" required />
          </label>
          <label>{t("login.password")}
            <PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
          </label>
          <ErrorNote message={error} />
          <button className="btn primary" disabled={busy}>{t("login.submit")}</button>
        </form>
      </main>
      <Footer />
    </div>
  );
}
