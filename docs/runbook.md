# Runbook

Environments: `staging` and `production` (separate Terraform state, separate AWS resources). Region `af-south-1`; database backups are also copied to `eu-west-1`.
Commands below use `ENV=staging|production`, `CLUSTER=fuelstation-$ENV`.

## Deploy
1. Build and push images (immutable tags, e.g. the git SHA) to the ECR repos in `terraform output`.
2. `terraform apply -var-file=envs/$ENV.tfvars` with the new `backend_image` / `frontend_image`.
3. Run the migrate task (creates DB roles, applies migrations as `app_owner`; safe to repeat):
   `aws ecs run-task --cluster $CLUSTER --launch-type FARGATE --task-definition fuelstation-$ENV-migrate --network-configuration "awsvpcConfiguration={subnets=[<private_subnets>],securityGroups=[<app_security_group>]}"`
4. Watch the ECS deployment. A failing deploy rolls back automatically (circuit breaker).
5. Check `https://<domain>/health/`.

Order matters: migrations are written to be backward compatible, so run the migrate task BEFORE the new web containers take traffic.

## Restore the database from a backup
Backups: automated daily + transaction logs (point-in-time restore) for 14 days (staging) / 35 days (production), encrypted; a copy is replicated to `eu-west-1`.
1. Decide the restore time (UTC). Never restore over the live instance; restore to a NEW instance.
2. `aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier fuelstation-$ENV --target-db-instance-identifier fuelstation-$ENV-restore --restore-time 2026-10-05T08:00:00Z --db-subnet-group-name fuelstation-$ENV-db --vpc-security-group-ids <db sg>`
   (Region lost? use `--source-db-instance-automated-backups-arn` from the `eu-west-1` replicated backups and restore there.)
3. Connect with the admin secret, check the data (`SELECT count(*) FROM operations_shift;`, newest `created_at`).
4. Switch the app: set `DB_HOST` to the restored endpoint (Terraform variable or task definition), deploy, run the migrate task (re-applies grants/policies; idempotent).
5. After a verified incident review, retire the old instance (take a final snapshot first). Never delete the only copy.
Practice this on staging every quarter and record the time it took.

## Export one tenant's data
- Self service: the owner presses "Export my data" (Settings). The zip (CSV + JSON) is built by the worker; the link lasts 24 hours.
- On their behalf (support): run `python manage.py export_tenant <slug>` as an ECS one-off task with the web image. It builds that tenant's export (only their rows are visible while it runs), audits it, and prints a time-limited link. The monthly copies in the versioned S3 bucket (`fuelstation-$ENV-exports-<account>/<tenant_id>/<yyyy-mm>/`) are another source.

## Suspend / reactivate / cancel a tenant manually
`python manage.py tenant_status <slug> suspend|reactivate|cancel` (run as an ECS one-off task with the web image). It is audited. Nothing is deleted. A payment webhook also reactivates a suspended tenant instantly; a cancelled tenant is never reactivated by a payment (check the Payment record and decide).
Run the daily billing cycle now: `python manage.py run_billing_cycle` (safe to repeat).

## A customer paid but has no account (`signup_conflict`)
The webhook recorded the event (`billing_paymentevent.result = signup_conflict`) and created nothing. Find it in the logs (`billing signup failed for event ...`), resolve the clash (e.g. the owner's email is already used), create the business from the platform screen, then record the payment against it manually. The payment company's event can be replayed only after the cause is fixed (events are idempotent by id).

## Messages are not arriving
Owners can see every message and its status in `/api/messages/` (`sent`, `failed`, `skipped`). `skipped` = no phone or channel set; `failed` = gateway error (see `detail`). Check `MESSAGE_GATEWAY_URL` and the token in Secrets Manager.

## Rotate secrets
Edit the secret in Secrets Manager (`fuelstation-$ENV/app`), then force a new deployment of web/worker/beat. Rotating `DJANGO_SECRET_KEY` signs every user out (tokens are signed with it). The RDS admin password is managed and rotated by AWS.

## Alarms (SNS email)
Downtime (no healthy API task), 5xx spike, DB CPU, DB low disk, scheduler (beat) not running, Redis CPU, RDS backup/failure/low-storage events.
If the scheduler is down, reminders, suspensions, summaries and alerts stop. Restart the beat service (exactly one must run).

## Single-server box (deploy/ kit): update to a new version
From the Windows PC: `.\deploy\push-to-aws.ps1` (see `deploy/README.md`). It deploys `origin/main` once CI is green: backup, install,
build, migrate on the owner role, check every table exists, restart, smoke test. Avoid shift changes (05:30-06:30, 13:30-14:30,
21:30-22:30); the server refuses then unless `-Force`. Roll back code with `-Rollback <commit>`; undo a migration by restoring
the backup taken at the start of the deploy (`deploy/restore.sh`).

## Delete a test or demo business (start afresh)
On the server, in `/opt/pumpwise/deploy`:
```
./backup.sh
docker compose run --rm -T migrate python manage.py purge_tenant                       # list: slug, demo/trial/REAL, status, name
docker compose run --rm -T migrate python manage.py purge_tenant <slug>                # dry run
docker compose run --rm -T migrate python manage.py purge_tenant <slug> --confirm <slug>   # add --real-business if not a demo
```
Logins, stations, shifts, everything of that business goes; the name and logins can be reused. To keep the business
and only refresh a demo's data, use `docker compose run --rm seed` (or `seed_demo --name ...`) instead.

## Pump system, pumps and nozzles
* The platform chooses the pump system on the "New business" form (`pump_system`): **shared** (one attendant per shift), **shared_multi** (several attendants share every pump) or **assigned** (each attendant works their own pumps). It grants the features it needs and can be changed on the business page (`PATCH /api/platform/tenants/<id>/pump-system/`) when no shift is open. New stations follow it. Removing a feature the system relies on moves it down (assigned → shared_multi → shared).
* Assigned pumps: managers and owners give each pump to an attendant on the **Pumps** screen. Attendants see only their pumps. Many attendants share one shift; each ends their own part (their numbers, their cash) and their pumps pass to the incoming attendant. For a pump in use, the manager either plans the handover (it passes when the person on it ends) or swaps it now with every nozzle's meter number.
* A pump has **at least one nozzle**; more can be added (Setup, "Add pump" and "Add a nozzle to a pump"). Each nozzle draws from any tank at the pump's station, and nozzles may share a tank. A pump's last nozzle cannot be removed; a pump with no nozzle shows "needs a nozzle". To change a nozzle's tank: `PATCH /api/nozzles/<id>/ {"tank": ...}`.
* Stations are added by the **owner** only. Each station has **one manager**: the owner sets each person's station in Settings, Team (a manager runs one station; attendants work at one station or all).
* An attendant who sees "No station is set up for you yet": check the owner added the station and, on assigned pumps, that a manager gave them pumps. "Could not load your stations: ..." shows the real reason (no signal, signed out, server error).
* Reports, "By attendant": litres per pump and fuel, cash handed in and the cash difference per attendant (`GET /api/reports/attendants/`).
* QR labels are switched off for now; pumps and tanks are labelled physically.

## Paid features and station policy
Platform screen, business page: "Paid features" switches and each station's attendants / pumps setting (API: `PATCH /api/platform/tenants/<id>/features/`, `PATCH /api/platform/tenants/<id>/stations/<station>/policy/`). Not possible while a shift is open at the station. Removing a feature moves the stations back to one attendant and shared pumps; history is kept.

## Demo and trial businesses
* Load or reload the demo: `docker compose run --rm seed` (demo compose file) or `python manage.py seed_demo --password ...`. It only ever wipes businesses marked `is_demo`.
* Nightly at 02:15 the Celery job `refresh_demo_tenants` regenerates the data up to yesterday and reloads every active demo business (logins stay the same).
* "Create trial" on the platform screen: a demo copy for a prospect, 14 days, owner password shown once. Daily at 06:30 expired trials are suspended, and removed 30 days later unless converted ("Convert to customer" drops the demo data and keeps the business).
* Demo businesses never send real SMS/WhatsApp/email (messages are logged as skipped) and checkout goes to `BILLING_SANDBOX_CHECKOUT_URL`.
