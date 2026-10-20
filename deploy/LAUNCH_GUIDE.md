# Pumpwise: Launch on AWS, Step by Step

Built by Tendeka & Elichi. This guide takes you from a folder on your computer to a live HTTPS website on AWS, using your credits. Plan on about 3 to 4 hours the first time, and part of that is waiting for DNS.

**How every step is written**
- **Do:** exactly what to click or type.
- **Why:** three reasons, so you understand the step and can fix it if it breaks.
- **Check:** how you know it worked before moving on.

**Two honest warnings**
1. AWS changes button labels now and then. The names in **bold** are what to look for. If a label is slightly different, look for the closest match.
2. Everything in `app/` is a tested **starter app**, not your prototype. That is deliberate (see A3). Launch the starter first, then swap your real app in.

**What was tested before you got this:** the starter app ran against a real PostgreSQL 16 using the kit's database role script (migrations as the owner role, app running as the limited role, admin login, `/health` turning 503 when the database is down and recovering, a Celery job running). The scripts pass a syntax check and `docker compose config` accepts the compose file. **Not tested:** the Caddyfile, the AWS clicks, and your real app. Expect small fixes.

---

## Your launch sheet (fill this in as you go)

| Item | Your value |
|---|---|
| Domain for the test server (for example `demo.yourdomain.com`) | |
| AWS account ID (12 digits, top-right menu) | |
| AWS region | |
| IAM admin users (one per founder) | |
| Credits expiry date (Billing > Credits) | |
| S3 backup bucket name | |
| EC2 instance ID | |
| Elastic IP address | |
| Key file name and where you saved it | |
| GitHub repo address | |
| Alert email addresses | |

---

# PART A. On your computer (about 45 minutes)

## A1. Understand the repository layout

Unzip `pumpwise-repo.zip`. This folder **is** your Git repository. Every file already sits where it belongs:

```
pumpwise/                        <- the repo root (one repo for everything)
├── .gitignore                   keeps secrets and junk out of Git
├── CLAUDE.md                    standing rules that Claude Code reads every session
├── PLAN.md                      the build plan, phase by phase
├── app/                         the Django project, built into ONE Docker image
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── requirements.txt
│   ├── manage.py
│   └── config/                  settings.py, urls.py, views.py, wsgi.py, celery.py
├── deploy/                      everything the SERVER needs (nothing here is app code)
│   ├── LAUNCH_GUIDE.md          this file
│   ├── README.md                short reference and the scaling path
│   ├── docker-compose.yml       the stack: Caddy, web, worker, beat, migrate, db, redis
│   ├── Caddyfile                HTTPS and routing
│   ├── .env.example             template for the secrets file (the real .env stays on the server)
│   ├── init-env.sh              creates .env with random secrets
│   ├── bootstrap.sh             one-time server setup
│   ├── backup.sh / restore.sh   nightly backup to S3, and the restore drill
│   ├── update.sh                safe deploy of a new version
│   ├── smoke.sh                 "is it alive" check
│   ├── db/01-roles.sh           creates the limited database role the app runs as
│   └── aws/                     files you paste into the AWS console
│       ├── iam-backup-policy.json
│       └── s3-lifecycle.json
├── docs/                        HOSTING_STRATEGY.md, DEMO_HOSTING_PLAN.md
└── demo/                        mock data generator and the hand-entry workbook
```

**Why this layout**
1. `app/` and `deploy/` are separate, so changing a server script never changes the app image, and the other way round.
2. One repo means one `git pull` on the server updates the code and the scripts together, and `docker-compose.yml` finds the app at `../app` with no extra settings.
3. Claude Code, you and Elichi can all find any file by its job, which matters when you add files later (Part D).

## A2. What each file is, and how it must be structured

| File | Lives in | What it does | Structure rules |
|---|---|---|---|
| `docker-compose.yml` | `deploy/` | Describes every container and how they connect | YAML. Each service has `restart`, a memory limit, and log rotation. Only Caddy publishes ports (80, 443). The database and Redis are never published. Service names (`web`, `worker`, `beat`, `migrate`, `db`, `redis`, `caddy`) are used by the scripts, so rename them only everywhere at once. |
| `Caddyfile` | `deploy/` | Gets the HTTPS certificate, serves `/static/`, passes everything else to `web` | One site block named after `{$DOMAIN}`. No secrets. |
| `.env.example` | `deploy/` | Lists every setting the stack needs | `NAME=value` lines, one per line, comments on their own lines. Placeholders look like `__PG__`. **Never put a real secret in this file.** |
| `.env` | `deploy/` **on the server only** | The real settings and passwords | Created by `init-env.sh`, permissions `600`, listed in `.gitignore`. |
| `init-env.sh` | `deploy/` | Fills `.env.example` with random secrets | Bash. Refuses to overwrite an existing `.env`. |
| `bootstrap.sh` | `deploy/` | Installs Docker, firewall, swap, AWS CLI, log rotation | Bash, run once with `sudo`. Safe to run twice. |
| `backup.sh` | `deploy/` | Dumps the database, checks the dump, uploads to S3 | Bash. Reads `.env`. Exits with an error if anything fails, so cron and the monitor notice. |
| `restore.sh` | `deploy/` | Restores a backup into a scratch database | Bash. Never touches the live database. |
| `update.sh` | `deploy/` | Backup, pull, build, migrate, restart, smoke test | Bash. Stops at the first error (`set -e`). |
| `smoke.sh` | `deploy/` | Shows container health and calls `/health` | Bash. Exit code 0 means healthy. |
| `01-roles.sh` | `deploy/db/` | Creates the limited `pumpwise_app` role | Runs once, when the database volume is first created. Mounted into the Postgres container. |
| `iam-backup-policy.json` | `deploy/aws/` | The permission policy for the server's role | JSON. Replace `BUCKET_NAME` in two places before pasting. |
| `s3-lifecycle.json` | `deploy/aws/` | Expires old backups after 35 days | JSON. Only the `db/` prefix. |
| `Dockerfile` | `app/` | Builds the app image | One image used by web, worker, beat and migrate. Runs as a non-root user. |
| `requirements.txt` | `app/` | Python packages | Version ranges tested together. Upgrade on purpose. |
| `config/settings.py` | `app/config/` | All Django settings | Every environment-specific value comes from an environment variable. Missing secrets stop the app at start (on purpose). |
| `config/views.py` | `app/config/` | `/health` and a home page | `/health` needs no login and never shows secrets. |
| `config/celery.py` | `app/config/` | Background job setup | App name must stay `config`, matching the compose file. |
| `CLAUDE.md` | repo root | Standing rules for Claude Code | Short. Layout, deployment rules, definition of done. |
| `.gitignore` | repo root | Keeps secrets out of Git | Must contain `.env`, `*.pem`, `*.dump`. |

## A3. Why we launch the starter app first

1. **It proves the whole path today:** domain, HTTPS, firewall, database roles, backups and monitoring all work, before any of your app code is involved.
2. **Problems show up one at a time.** If the starter works and your prototype later fails, the fault is in the prototype, not the server.
3. **Your credits start working for you now**, and Claude Code gets a working deployment to build into instead of guessing.

## A4. Put the repo on GitHub

**Do**
1. Create a **private** repository named `pumpwise` on GitHub (or your team's organisation).
2. In a terminal, inside the unzipped `pumpwise-repo` folder:
   ```bash
   git init
   git add .
   git update-index --chmod=+x deploy/*.sh deploy/db/01-roles.sh   # keeps scripts runnable, matters on Windows
   git commit -m "Pumpwise: starter app and AWS deploy kit"
   git branch -M main
   git remote add origin git@github.com:YOUR_ORG/pumpwise.git      # or the https address GitHub shows
   git push -u origin main
   ```
3. Open the repo on GitHub and check that `.env` and any `.pem` file are **not** there.

**Why**
1. The server pulls code from Git, and `update.sh` depends on it.
2. Git keeps every version, so you can roll back a bad deploy.
3. Private repo plus `.gitignore` keeps secrets out of anyone else's hands.

**Check:** the repo on GitHub shows `app/`, `deploy/`, `docs/`, `demo/`, `PLAN.md`, `CLAUDE.md`.

## A5. Get a domain name

**Do:** use a domain you already own, or buy one from any registrar. Decide the test address now, for example `demo.yourdomain.com`.

**Why**
1. HTTPS certificates are issued to names, not bare IP addresses.
2. Phones need HTTPS to install the app (PWA), use the camera, and work offline.
3. You can move to a new server later by changing one DNS record.

*If you have no domain today:* a free name that points at your IP, such as `203-0-113-10.sslip.io` (your Elastic IP with dashes), often works for a first test. The certificate service can rate-limit shared names, so use a real domain before showing anyone.

---

# PART B. The AWS console (about 2 hours)

## B1. Lock down the root account

**Do**
1. Sign in at aws.amazon.com with the email you used to create the account (this is the **root user**).
2. Click your account name in the top-right corner, then **Security credentials**.
3. Under **Multi-factor authentication (MFA)** click **Assign MFA device**. Name it `root-phone`, choose **Authenticator app**, scan the QR code with your phone, and type two consecutive codes.

**Why**
1. The root user cannot be limited. Whoever gets its password owns the account and your credits.
2. Stolen AWS accounts are often used to mine cryptocurrency, which burns credits within days.
3. It takes five minutes and protects everything else you do.

**Check:** the Security credentials page lists an assigned MFA device.

## B2. Create admin users for daily work (one per founder)

**Do**
1. Still as root, click your account name, then **Account**. Find **IAM user and role access to Billing information**, click **Edit**, tick **Activate IAM access**, and **Update**. *(Without this, your admin user cannot open the Billing pages.)*
2. In the search bar type **IAM** and open it. Go to **Users**, then **Create user**.
3. User name: `tendeka-admin`. Tick **Provide user access to the AWS Management Console**, choose **I want to create an IAM user**, set a custom password, then **Next**.
4. Choose **Attach policies directly**, search for **AdministratorAccess**, tick it, **Next**, **Create user**.
5. Copy the **Console sign-in URL** shown at the end into your launch sheet.
6. Open the new user, go to **Security credentials**, and **Assign MFA device** as in B1.
7. Repeat for `elichi-admin`.
8. Sign out of root. From now on sign in with the console sign-in URL and your own admin user.

**Why**
1. Root is for emergencies only. Daily work with a limited, switch-off-able user is much safer.
2. One user per person means you can see who did what, and remove one person without touching the other.
3. Each user has its own MFA, so one lost phone does not open the account.

**Check:** you can sign in as `tendeka-admin`, and the **Billing and Cost Management** page opens.

## B3. Choose the region (and stay in it)

**Do:** click the region name in the top-right corner and choose **Europe (Ireland) eu-west-1**. Write it in your launch sheet.

**Why**
1. Everything you create (server, bucket, IP) lives in one region. Changing later means rebuilding.
2. Ireland needs no opt-in, so you start today. Cape Town (`af-south-1`) is an opt-in region and I could not confirm its service availability or prices, so compare it later for production.
3. For a test server with offline-first apps, a few extra milliseconds do not matter.

**Check:** the region name appears in the top bar. **If something seems to be missing in the console later, check the region first.**

## B4. Set spending alerts and look at your credits

**Do**
1. Search **Billing and Cost Management**. Open **Credits** and note the balance and the **expiry date** in your launch sheet.
2. Open **Budgets**, then **Create budget**. Choose **Use a template (simplified)** and **Monthly cost budget**. Name it `pumpwise-monthly`, amount `25`, add both founders' emails, and **Create budget**.
3. Open **Billing preferences**. Under **Alert preferences** click **Edit**, switch on the **AWS Free Tier alerts** and enter both emails. Save.

**Why**
1. Credits end silently on their expiry date or when used up, so you need to see both numbers.
2. One mistake, such as a large server or a NAT gateway, can burn credits in days. An alert gives you time to react.
3. Budgets may count costs before credits are applied, so an alert does not always mean you will be billed. Check the Credits page when one arrives.

**Check:** you received a confirmation email, and Credits shows a balance.

## B5. Create the backup bucket (S3)

**Do**
1. Search **S3**, **Create bucket**.
2. Bucket name: `pumpwise-test-backups-` plus 6 random digits (names are global). Write it in your launch sheet. Region should already be Ireland.
3. Leave **Block all public access** ticked. Under **Bucket Versioning** choose **Enable**. Leave default encryption (SSE-S3) as it is. **Create bucket**.
4. Open the bucket, go to **Management**, **Create lifecycle rule**. Name `expire-test-db-backups`. Choose **Limit the scope using filters** and set the prefix `db/`. Tick **Expire current versions of objects** (35 days), **Permanently delete noncurrent versions of objects** (35 days), and **Delete incomplete multipart uploads** (7 days). **Create rule**.
   *(This matches `deploy/aws/s3-lifecycle.json`.)*

**Why**
1. Backups must live **off the server**. If the server dies, the backups survive.
2. Versioning and blocked public access protect against overwrites and accidental leaks.
3. The lifecycle rule stops old backups piling up as a surprise cost.

**Check:** the bucket shows **Versioning: Enabled**, and the lifecycle rule appears under Management.

## B6. Give the server permission to use the bucket (IAM role)

**Do**
1. Open the file `deploy/aws/iam-backup-policy.json`. Replace `BUCKET_NAME` with your bucket name in **both** places.
2. In **IAM**, go to **Policies**, **Create policy**, choose the **JSON** tab, paste the edited text, **Next**. Name it `pumpwise-backup`. **Create policy**.
3. Go to **Roles**, **Create role**. Trusted entity type: **AWS service**, use case **EC2**, **Next**. Search and tick `pumpwise-backup`, **Next**. Role name `pumpwise-server`. **Create role**.

**Why**
1. The server proves who it is through the role, so **no access keys are stored on the disk**. Keys leak through Git, screenshots and chat messages.
2. The policy allows only upload, download and list on this one bucket. There is **no delete permission**, so a bug or an intruder cannot wipe your backups.
3. Roles are also how the production setup (ECS) will work later, so you learn the pattern once.

**Check:** the role `pumpwise-server` lists the `pumpwise-backup` policy.

## B7. Launch the server (EC2)

**Do:** search **EC2**, then **Launch instance**, and fill in:
1. **Name:** `pumpwise-test`.
2. **Application and OS Image:** **Ubuntu**, **Ubuntu Server 24.04 LTS**, 64-bit (x86).
3. **Instance type:**
   - Account created **on or after 15 July 2025:** choose **t3.small** (2 GB RAM). AWS lists `t3.micro`, `t3.small`, `t4g.micro`, `t4g.small`, `c7i-flex.large` and `m7i-flex.large` as free-plan eligible for these accounts.
   - Older account: only `t3.micro` (1 GB) is eligible. It runs the starter app with the swap file that `bootstrap.sh` adds, but it will be tight for the real app.
   - Pick one marked **Free tier eligible**.
4. **Key pair:** **Create new key pair**, name `pumpwise-key`, type RSA, format `.pem`. The file downloads **once**. Store it somewhere safe, never in the repo.
5. **Network settings:** click **Edit**. **Create security group** named `pumpwise-sg`. Rules: **SSH** (port 22), source **My IP**. Add **HTTP** (80) from **Anywhere**. Add **HTTPS** (443) from **Anywhere**.
6. **Configure storage:** `30` GiB, **gp3**.
7. **Advanced details:** **IAM instance profile** `pumpwise-server`. **Metadata version**: **V2 only (token required)**.
8. Click **Launch instance**, then **View all instances**.

**Why**
1. Ubuntu 24.04 LTS is what `bootstrap.sh` is written for, and it has long-term security updates.
2. 2 GB of RAM is the smallest size that comfortably runs the web app, worker, Postgres and Redis together.
3. The firewall opens only web ports to the world. SSH is only for your IP, and V2 metadata blocks a common credential-theft trick.

**Check:** **Instance state** shows **Running** and **Status check** shows **2/2 checks passed** (a few minutes).

## B8. Give it a permanent address (Elastic IP)

**Do**
1. In the EC2 left menu choose **Elastic IPs**, then **Allocate Elastic IP address**, then **Allocate**.
2. Tick the new address, then **Actions**, **Associate Elastic IP address**. Choose your instance `pumpwise-test`, and **Associate**.
3. Write the address in your launch sheet.

**Why**
1. A normal public IP changes when you stop and start the server, which would break your domain.
2. Your DNS record and SSH rule need one stable address.
3. Certificates are issued to the domain, so the address must not move.

**Note:** AWS charges for public IPv4 addresses, and for an Elastic IP that is not attached to anything. Release it when you tear the server down (Part E).

**Check:** the instance page now shows the Elastic IP as its public address.

## B9. Point your domain at the server (DNS)

**Do**
1. Sign in to wherever your domain's DNS is managed (the registrar, or Route 53).
2. Add an **A record**. Name: `demo` (or whatever you chose). Value: your Elastic IP. TTL: `300`. Save.
3. From your computer run `nslookup demo.yourdomain.com` until it shows your Elastic IP. This can take from 5 minutes to an hour.

**Why**
1. Caddy cannot get your HTTPS certificate until the name points at the server.
2. A low TTL (300 seconds) means mistakes are fixed fast.
3. Waiting now, while you do B10 and B11, wastes no time.

**Check:** `nslookup` shows the Elastic IP.

## B10. Connect to the server (SSH)

**Do**
1. **Mac or Linux:**
   ```bash
   chmod 400 ~/Downloads/pumpwise-key.pem
   ssh -i ~/Downloads/pumpwise-key.pem ubuntu@YOUR_ELASTIC_IP
   ```
2. **Windows (PowerShell):** `ssh -i C:\Users\YOU\Downloads\pumpwise-key.pem ubuntu@YOUR_ELASTIC_IP`. If it complains about an **UNPROTECTED PRIVATE KEY FILE**, run `icacls "C:\Users\YOU\Downloads\pumpwise-key.pem" /inheritance:r` and then `icacls "C:\Users\YOU\Downloads\pumpwise-key.pem" /grant:r "$($env:USERNAME):(R)"`.
3. Type `yes` to the fingerprint question.

**Why**
1. Every server task from here happens in this terminal.
2. Key-based login is far stronger than a password.
3. It also tests your firewall rule.

**If it hangs:** your home IP changed. In the EC2 console open **Security Groups**, `pumpwise-sg`, **Edit inbound rules**, set SSH to **My IP** again.

**Check:** you see a prompt like `ubuntu@ip-172-31-...:~$`.

## B11. Install and start Pumpwise on the server

Run these on the server, in order.

**Do**
1. Install Git, then create a read-only deploy key so the server can pull your code:
   ```bash
   sudo apt-get update -y && sudo apt-get install -y git
   ssh-keygen -t ed25519 -C "pumpwise-server" -f ~/.ssh/pumpwise_deploy -N ""
   cat ~/.ssh/pumpwise_deploy.pub
   ```
2. Copy that line. On GitHub open your repo, **Settings**, **Deploy keys**, **Add deploy key**. Title `pumpwise-test-server`, paste the key, leave **Allow write access** unticked, **Add key**.
3. Tell SSH to use it, then clone:
   ```bash
   printf 'Host github.com\n  IdentityFile ~/.ssh/pumpwise_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
   chmod 600 ~/.ssh/config
   ssh-keyscan github.com >> ~/.ssh/known_hosts
   sudo mkdir -p /opt/pumpwise && sudo chown ubuntu:ubuntu /opt/pumpwise
   git clone git@github.com:YOUR_ORG/pumpwise.git /opt/pumpwise
   ```
4. One-time server setup, then log out and back in:
   ```bash
   cd /opt/pumpwise/deploy
   chmod +x *.sh db/01-roles.sh
   sudo ./bootstrap.sh
   exit
   ```
   Reconnect with the same `ssh` command.
5. Create the settings file:
   ```bash
   cd /opt/pumpwise/deploy
   ./init-env.sh          # asks for your domain, creates .env with random secrets
   nano .env              # set AWS_REGION=eu-west-1 and BACKUP_BUCKET=your bucket name
   ```
   Save in nano with `Ctrl+O`, `Enter`, then `Ctrl+X`.
6. Start it, in this order:
   ```bash
   docker compose up -d db redis        # first start creates the limited app database role
   docker compose ps                    # wait until db shows "healthy"
   docker compose build                 # first build takes a few minutes
   docker compose run --rm migrate      # database tables and static files, as the owner role
   docker compose up -d                 # start everything
   ./smoke.sh                           # containers healthy and https://DOMAIN/health returns 200
   docker compose run --rm migrate python manage.py createsuperuser
   ```

**Why**
1. A read-only deploy key lets the server **pull** your code but never change it.
2. `bootstrap.sh` sets up Docker, the firewall, swap memory (so a memory spike slows the server instead of killing the database), and log rotation (a full disk is the most common way small servers die).
3. The order matters. The database must exist first so it creates the limited role. Migrations must finish before the app starts. And the smoke test proves the public path works from outside.

**Check**
- Open `https://demo.yourdomain.com` in a browser. You see the Pumpwise page.
- `https://demo.yourdomain.com/health` shows `{"status": "ok", ...}`.
- `https://demo.yourdomain.com/admin/` lets you sign in with the superuser you just made.
- Prove the worker runs: `docker compose run --rm migrate python manage.py shell -c "from config.celery import ping; ping.delay()"` then `docker compose logs --tail 20 worker` shows the `ping` task succeeded.

**If HTTPS fails:** check `docker compose logs caddy`. The usual cause is DNS not pointing at the Elastic IP yet, or ports 80/443 not open in the security group.

## B12. Turn on backups and prove you can restore

**Do**
1. Run one backup by hand: `./backup.sh`. In the S3 console open your bucket, **db/**, and find the new file.
2. Schedule it: `crontab -e` (choose nano if asked), add this line at the bottom, and save:
   ```
   15 2 * * * /opt/pumpwise/deploy/backup.sh >> /home/ubuntu/backup.log 2>&1
   ```
   Check with `crontab -l`. (02:15 server time, which is UTC. That is 04:15 in Zimbabwe, outside shift changes.)
3. Create a free check on a service such as Healthchecks.io (check its current free limits). Set the period to 1 day with a grace time of 2 hours, add both emails, copy its **ping URL**, and put it in `.env` as `HEALTHCHECK_URL=`. Run `./backup.sh` again and watch the check turn green.
4. Run the restore drill: `./restore.sh latest`. Read the row counts. Then drop the scratch copy with the command it prints.

**Why**
1. A backup you have never restored is a hope, not a backup. This finds permission and bucket mistakes while there is no real data at stake.
2. The ping URL alerts you when backups **stop** happening, which is the failure nobody notices.
3. Doing this now makes it routine before real customers arrive.

**Check:** a dump file exists in S3, the check is green, and the restore drill printed table counts.

## B13. Set up alerts so you hear before owners do

**Do**
1. In **EC2**, **Instances**, select `pumpwise-test`, open the **Status and alarms** tab, and click **Create status check alarm**.
2. Turn on **Alarm notification**. Under **Send a notification to** choose **Create topic**, name it `pumpwise-alerts`, and add both founders' emails. Leave the **alarm action** off. **Create**.
3. Open both inboxes and click **Confirm subscription** in the email from AWS.
4. Create a free HTTPS monitor on an uptime service (UptimeRobot, Better Stack or similar). URL: `https://demo.yourdomain.com/health`, every 5 minutes, alert both emails.

**Why**
1. The AWS alarm catches the **machine** failing.
2. The external monitor catches **DNS, HTTPS and app** failures that AWS cannot see.
3. Knowing first lets you fix problems before an owner phones you.

**Check:** both subscriptions say **Confirmed**, and the uptime monitor shows **Up**.

## B14. Final launch check (all eight must pass)

| # | Test | Pass looks like |
|---|---|---|
| 1 | Browser to `https://DOMAIN` | Pumpwise page, padlock shown |
| 2 | `https://DOMAIN/health` | `{"status": "ok"...}` |
| 3 | `https://DOMAIN/admin/` | Superuser can sign in, styling loads |
| 4 | `./smoke.sh` on the server | All containers up, `OK` |
| 5 | Open the site on a phone using mobile data | Loads in a few seconds |
| 6 | `./backup.sh` | Dump appears in S3 |
| 7 | `./restore.sh latest` | Row counts printed |
| 8 | Stop the web container (`docker compose stop web`), wait 5 minutes | Uptime monitor emails you. Then `docker compose start web` |

**You are live on AWS.** Write down the date in your launch sheet.

---

# PART C. Swapping in your real app

**Do**
1. Give Claude Code this instruction: *"Read CLAUDE.md and PLAN.md. Phase 0: review my prototype and merge it into `app/` while keeping the deployment rules in CLAUDE.md. Do not change `deploy/` unless needed, and tell me if you do."*
2. After each phase, make sure `docker compose config` still passes in `deploy/` and the tests pass, then `git push`.
3. On the server, deploy with:
   ```bash
   cd /opt/pumpwise/deploy
   ./update.sh
   ```
4. **Rollback:** `git -C ../app checkout <previous-commit>` then `./update.sh` again. If a migration went wrong, restore the backup that `update.sh` took in step 1 (see `restore.sh`).

**Why**
1. `CLAUDE.md` makes every session follow the same deployment rules, so the app keeps running on this server.
2. `update.sh` backs up **before** every deploy.
3. Small, frequent deploys are easier to roll back than one big one. Avoid shift-change times (06:30 to 07:30 and 17:30 to 18:30 Zimbabwe time; shifts run 07:00-18:00 and 18:00-07:00).

**Contract your real app must keep:** `/health` public, settings only from environment variables, WSGI module `config.wsgi`, Celery app `config`, the app connecting as the limited database role, and no important files written to the container's disk.

---

# PART D. Files you will add later: where they go and how to structure them

| File or folder | Goes in | Structure | When |
|---|---|---|---|
| `ci.yml` | `.github/workflows/` | GitHub Actions YAML. Job 1: start Postgres, run the tests including the tenant isolation suite. Job 2: build the Docker image. Secrets only in GitHub Secrets, never in the file. | Phase 1, with the first tests |
| Terraform files | `deploy/terraform/` | `main.tf`, `variables.tf`, `outputs.tf`, `modules/` (network, rds, ecs, alb, s3), and `envs/staging/` and `envs/production/` each with its own `terraform.tfvars`. Remote state in S3 with locking. Never commit `*.tfstate` or secrets. | Stage 0 production, when the first pilot is near |
| `.env.production.example` | `deploy/` | Like `.env.example`, but secrets are named references to AWS Secrets Manager, not values. | Stage 0 |
| Load test scripts | `deploy/loadtest/` | `shift_storm.js`, `dashboard_rush.js`, `soak.js` (k6), plus a `README.md` with the pass criteria from `docs/HOSTING_STRATEGY.md`. | Before the first pilot |
| Runbooks | `deploy/runbooks/` | One short file per situation: `restore-database.md`, `suspend-tenant.md`, `incident-quick-guide.md`. Each has: when to use it, numbered steps, how to verify. | Before the first pilot |
| `seed_demo` command | `app/core/management/commands/seed_demo.py` | Loads `demo/` mock data through the Django ORM so Row-Level Security applies. Idempotent. Only touches tenants flagged `is_demo`. | Phase 12 |
| Generated mock data | `demo/mock_data/` | Output of `generate_mock_data.py`. Git-ignored, regenerated nightly. | Phase 12 |
| More docs | `docs/` | Plain Markdown. One topic per file. | Any time |

**Five rules for every new file**
1. **One job per file**, and the file name says the job.
2. **No secrets in Git, ever.** Secrets live in `.env` on the server or in AWS Secrets Manager.
3. Scripts start with `set -euo pipefail`, stop on the first error, and print what they did.
4. Anything that changes servers or data takes a backup first, or says clearly that it does not.
5. If you add a file that the server needs, add a row to this table so everyone knows where it goes.

---

# PART E. Costs, stopping, and tearing down

**What costs money on this setup:** the EC2 server, its 30 GB disk, the Elastic IP, S3 storage (small), and data going out of AWS. Check **Billing > Cost Explorer** and **Credits** every week.

**To stop spending while idle:** stopping the server still charges for the disk and the Elastic IP. To stop completely, tear down in this order:
1. Take a final `./backup.sh` and download the dump if you need it.
2. **EC2:** terminate the instance.
3. **Elastic IPs:** disassociate, then **Release**.
4. **S3:** empty the bucket (including old versions), then delete it.
5. **CloudWatch/SNS:** delete the alarm and the topic.
6. Remove the DNS record.

**Why the order matters:** an Elastic IP released too late, or a forgotten bucket or alarm, keeps costing money after the server is gone.

---

# PART F. If something goes wrong

| Problem | What to check |
|---|---|
| `ssh` hangs or times out | Your IP changed: edit the security group's SSH rule to **My IP**. Instance must be **Running**. |
| `Permission denied (publickey)` | Wrong key file, wrong user (it is `ubuntu`), or the key file is not protected (`chmod 400`). |
| `git clone` is refused | The deploy key was not added to the repo, or `~/.ssh/config` is missing. |
| HTTPS not working | `nslookup` must show the Elastic IP. Ports 80 and 443 must be open. Read `docker compose logs caddy`. |
| Site shows 502 Bad Gateway | The web container is not healthy: `docker compose logs --tail 100 web`. |
| `web` restarts in a loop | Missing setting in `.env`, or `ALLOWED_HOSTS` lacks `localhost` and `web`. |
| Admin page has no styling | Run `docker compose run --rm migrate` again (it collects static files). |
| Backup fails with AccessDenied | The instance is missing the `pumpwise-server` role (EC2 > Actions > Security > Modify IAM role), or the bucket name in `.env` or the policy is wrong. |
| Out of memory | Set `WEB_WORKERS=1` and `CELERY_CONCURRENCY=1` in `.env`, then `docker compose up -d`. Or resize the instance. |
| Disk full | `df -h`, `docker system df`, `docker image prune`. |
| AWS bill higher than expected | Cost Explorer, group by **Service**. Look for NAT gateway, load balancer, RDS or unattached IPs. |

---

# Time plan for launch day

| Block | Steps | About |
|---|---|---|
| 1 | A1 to A5: repo, GitHub, domain | 45 min |
| 2 | B1 to B6: security, region, alerts, bucket, role | 45 min |
| 3 | B7 to B9: server, IP, DNS (start the DNS wait) | 30 min |
| 4 | B10 to B11: connect, install, start | 60 to 90 min |
| 5 | B12 to B14: backups, alerts, final check | 45 min |

AWS pages, prices and free-plan rules change. Check every cost in the AWS console before you rely on it.
