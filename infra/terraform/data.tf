# ---- PostgreSQL (RDS): encrypted, automated backups, point-in-time restore, cross-region copy ----
resource "aws_kms_key" "db" {
  description         = "${local.name} database encryption"
  enable_key_rotation = true
}

resource "aws_db_subnet_group" "db" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.db[*].id
}

resource "aws_db_instance" "main" {
  identifier                      = local.name
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_storage_gb
  max_allocated_storage           = var.db_storage_gb * 4
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.db.arn
  db_name                         = "fuelstation"
  username                        = "fs_admin"
  manage_master_user_password     = true # the admin password lives in Secrets Manager, rotated by AWS
  db_subnet_group_name            = aws_db_subnet_group.db.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  publicly_accessible             = false
  multi_az                        = var.db_multi_az
  backup_retention_period         = var.db_backup_days # automated backups + point-in-time restore
  backup_window                   = "00:30-01:30"
  maintenance_window              = "sun:02:00-sun:03:00"
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = !var.deletion_protection
  final_snapshot_identifier       = var.deletion_protection ? "${local.name}-final" : null
  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true
}

# Cross-region copy of automated backups (restore even if the whole region is lost)
resource "aws_kms_key" "db_backup" {
  provider            = aws.backup
  description         = "${local.name} backup copy"
  enable_key_rotation = true
}
resource "aws_db_instance_automated_backups_replication" "copy" {
  provider               = aws.backup
  source_db_instance_arn = aws_db_instance.main.arn
  kms_key_id             = aws_kms_key.db_backup.arn
  retention_period       = var.db_backup_days
}

# ---- Redis (Celery broker + cache) ----
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}
resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = local.name
  description                = "${local.name} broker and cache"
  engine                     = "redis"
  node_type                  = var.redis_node_type
  num_cache_clusters         = var.environment == "production" ? 2 : 1
  automatic_failover_enabled = var.environment == "production"
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
}

# ---- S3: monthly exports (versioned) and private photo storage ----
resource "aws_s3_bucket" "exports" { bucket = "${local.name}-exports-${data.aws_caller_identity.me.account_id}" }
resource "aws_s3_bucket" "media" { bucket = "${local.name}-media-${data.aws_caller_identity.me.account_id}" }
data "aws_caller_identity" "me" {}

resource "aws_s3_bucket_versioning" "exports" {
  bucket = aws_s3_bucket.exports.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "AES256" } }
}
resource "aws_s3_bucket_public_access_block" "exports" {
  bucket                  = aws_s3_bucket.exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls       = true
  restrict_public_buckets = true
}
# Old versions are kept 13 months, then removed. Current monthly exports stay.
resource "aws_s3_bucket_lifecycle_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    id     = "keep-13-months-of-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 400 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

# ---- Secrets (values never in git or tfvars) ----
resource "random_password" "django" {
  length  = 64
  special = false
}
resource "random_password" "db_app" {
  length  = 32
  special = false
}
resource "random_password" "db_owner" {
  length  = 32
  special = false
}
resource "random_password" "webhook" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "app" { name = "${local.name}/app" }
resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    DJANGO_SECRET_KEY      = random_password.django.result
    DB_APP_PASSWORD        = random_password.db_app.result
    DB_OWNER_PASSWORD      = random_password.db_owner.result
    BILLING_WEBHOOK_SECRET = random_password.webhook.result
    MESSAGE_GATEWAY_TOKEN  = "set-me-in-the-console"
  })
  lifecycle { ignore_changes = [secret_string] } # real values (e.g. the SMS gateway token) are edited in the console
}
