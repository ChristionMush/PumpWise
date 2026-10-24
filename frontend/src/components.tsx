import { useId, useState, type InputHTMLAttributes } from "react";
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

/** A password box with an eye button that shows what was typed, so people can check it before they submit. */
export function PasswordInput(props: Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  const [shown, setShown] = useState(false);
  const toggle = () => setShown((v) => !v);
  return (
    <span className="pw">
      <input {...props} type={shown ? "text" : "password"} autoCapitalize="off" autoCorrect="off" spellCheck={false} />
      {/* a span with the button role, not a <button>: these boxes sit inside a <label>, which would also name a real button */}
      <span role="button" tabIndex={0} className="pw-toggle" aria-label={t(shown ? "pw.hide" : "pw.show")} aria-pressed={shown} title={t(shown ? "pw.hide" : "pw.show")}
        onClick={(e) => { e.preventDefault(); toggle(); }}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}>
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
          <circle cx="12" cy="12" r="3" />
          {shown && <path d="M4 4l16 16" />}
        </svg>
      </span>
    </span>
  );
}

/** Cash with cents: notes are counted in whole dollars, so this is a typing slip. */
export const hasCents = (v: string | undefined) => Boolean(v) && Number(v) !== Math.trunc(Number(v));

/** One payment type's box. Cash is notes, counted in whole dollars: the box takes no cents. */
export function CashField({ type, label, value, onChange }: { type: string; label: string; value: string; onChange: (v: string) => void }) {
  const whole = type === "cash";
  const hint = useId();
  const cents = whole && hasCents(value);
  return (
    <div className="form-field">
      <label>{label}
        <input inputMode={whole ? "numeric" : "decimal"} value={value} aria-describedby={whole ? hint : undefined}
          onChange={(e) => onChange(e.target.value.replace(",", "."))} />
      </label>
      {whole && <span id={hint} className={cents ? "error small" : "muted small"}>{cents ? "Cash is counted in whole dollars: remove the cents" : "Cash is counted in whole dollars"}</span>}
    </div>
  );
}
