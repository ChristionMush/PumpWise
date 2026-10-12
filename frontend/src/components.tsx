import { useState } from "react";
import { t } from "./i18n";
import type { Credentials } from "./types";

export function Footer() {
  return <footer className="footer">{t("builtBy")}</footer>;
}

export function Badge({ kind, children }: { kind: string; children: string }) {
  return <span className={`badge badge-${kind}`}>{children.replace("_", " ")}</span>;
}

export function ErrorNote({ message }: { message: string | null }) {
  return message ? <p role="alert" className="error">{message}</p> : null;
}

/** Shown once after creating a business or resetting an owner's password. */
export function CredentialsCard({ credentials, title }: { credentials: Credentials; title: string }) {
  const [copied, setCopied] = useState(false);
  const text = `Login: ${credentials.login}\nTemporary password: ${credentials.temporary_password}`;
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true); } catch { /* user can still read it on screen */ }
  };
  return (
    <section className="card credentials" aria-label="Owner login">
      <h2>{title}</h2>
      <dl>
        <dt>Login</dt><dd>{credentials.login}</dd>
        <dt>Temporary password</dt><dd className="mono" data-testid="temp-password">{credentials.temporary_password}</dd>
      </dl>
      <p className="warn">Write this down or copy it now. It is shown only once. The owner must choose their own password at first sign in.</p>
      <button className="btn" onClick={() => void copy()}>{copied ? t("common.copied") : t("common.copy")}</button>
    </section>
  );
}
