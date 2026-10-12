# Security review checklist (before real tenants)

Tick each item and record who checked it and when.

**Tenant isolation**
- [ ] CI is green, including `tests/test_isolation_everything.py` (walks every tenant table) and `tests/test_rls_guard.py`.
- [ ] In staging: `app_user` is not superuser, has no BYPASSRLS, owns no tables (`\du`, `\dt`); it has no DELETE/TRUNCATE.
- [ ] `ATOMIC_REQUESTS` is true for the default database alias.
- [ ] Migrations ran as `app_owner` (migrate task); the web tasks cannot alter the schema.

**Secrets and access**
- [ ] No secret in git, tfvars or task definitions (only Secrets Manager references). `git log -p | grep -i secret` is clean.
- [ ] `DJANGO_DEBUG=false`, `DJANGO_ALLOWED_HOSTS` is the real domain, HSTS and SSL redirect are on.
- [ ] Billing webhook secret set; a request with a bad signature returns 400 (try it).
- [ ] Platform admin accounts use strong unique passwords; only Tendeka & Elichi staff have them.
- [ ] AWS: MFA on all console users, least-privilege IAM, CloudTrail on.

**Network and data**
- [ ] Database and Redis are in private subnets; no public IP; security groups only allow the app.
- [ ] TLS everywhere (ALB certificate valid, TLS 1.2+; Redis in-transit encryption on; RDS storage encrypted).
- [ ] S3 buckets block public access, are versioned, encrypted; media is served only through the authenticated API.
- [ ] Backups: restore tested on staging within the last quarter and timed.

**Operations**
- [ ] Alarms deliver to a monitored inbox (send a test). Exactly one beat scheduler runs.
- [ ] Runbook rehearsed: restore, tenant export, manual suspend/reactivate.
- [ ] Message gateway and payment provider credentials set; one real test message and one test payment completed.
- [ ] Dependency audit: `pip-audit` and `npm audit` reviewed; images scanned (ECR scan on push).
- [ ] Rate limits on login/refresh active (`THROTTLE_LOGIN`, `THROTTLE_REFRESH`); consider per-account limits.

**Known limits to accept or fix**
- RLS defends against application bugs, not arbitrary SQL injection (the ORM is parameterised; review any raw SQL).
- Tokens are stored in browser `localStorage` (so attendants survive power cuts); an XSS bug would expose them. Keep the content-security policy strict.
