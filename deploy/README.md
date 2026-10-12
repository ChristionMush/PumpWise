# deploy/

Everything the AWS server needs. **Start with `LAUNCH_GUIDE.md`**: it walks through every step with the reasons behind it.

Built by Tendeka & Elichi.

## Files
| File | Job |
|---|---|
| `LAUNCH_GUIDE.md` | The step-by-step launch on AWS, with file locations and structure |
| `docker-compose.yml` | The stack: Caddy, web, worker, beat, migrate, db, redis |
| `Caddyfile` | HTTPS and routing |
| `.env.example` | Template for the settings file (real `.env` is made on the server by `init-env.sh`) |
| `init-env.sh`, `bootstrap.sh` | Create `.env` with random secrets, and one-time server setup |
| `backup.sh`, `restore.sh` | Nightly backup to S3, and the restore drill |
| `update.sh`, `smoke.sh` | Safe deploy, and the "is it alive" check |
| `db/01-roles.sh` | Creates the limited database role the app runs as (Row-Level Security applies to it) |
| `aws/` | JSON to paste into the AWS console: backup policy and bucket lifecycle |

## Scaling path (what changes, what stays the same)
The app reads its settings only from environment variables, keeps no files on its own disk (uploads go to S3), logs to the console, and exposes `/health`. That makes each step below a configuration move, not a rewrite.

| Step | Trigger | What changes | What stays the same |
|---|---|---|---|
| **A. Bigger box** | Memory or CPU above 70% for a sustained period | Resize the instance (stop it, change the type, start it) | Everything |
| **B. Real production (Stage 0 in `docs/HOSTING_STRATEGY.md`)** | First pilot with real station data | New AWS account or VPC. Database moves to **RDS Multi-AZ** (`pg_dump` from this box, `pg_restore` to RDS, change `DATABASE_URL`). Redis to ElastiCache (`REDIS_URL`). Images go to ECR and run on **ECS Fargate** (compose services become ECS tasks). Caddy is replaced by an **Application Load Balancer** with an AWS certificate, plus CloudFront for the PWA files. Secrets move to Secrets Manager. Nightly dumps are replaced by RDS backups with point-in-time restore and cross-region snapshot copy. | The same Docker images, the same environment variable names, the same migrations, the same restore drill |
| **C. Growth** | Triggers in `docs/HOSTING_STRATEGY.md` section 4 | Autoscale web tasks, add RDS Proxy and a read replica, partition the large tables | The same images |

Moving from this box to RDS takes a short maintenance window: stop web and worker, take a final dump, restore it into RDS, change the URLs, start the new setup, check `/health`. Rehearse it once with demo data before you need it.

**Production note:** here the database owner role is a superuser, which is acceptable inside this private container but not in production. In RDS use a normal owner role and add `ALTER TABLE ... FORCE ROW LEVEL SECURITY` to every tenant table, so even the table owner is bound by the policies.
