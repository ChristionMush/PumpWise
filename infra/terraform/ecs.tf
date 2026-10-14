resource "aws_ecr_repository" "backend" {
  name                 = "${local.name}-backend"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_repository" "frontend" {
  name                 = "${local.name}-frontend"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}
resource "aws_cloudwatch_log_group" "app" {
  name              = "/fuelstation/${var.environment}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.app.arn, aws_db_instance.main.master_user_secret[0].secret_arn] }]
  })
}
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "task_s3" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"]
      Resource = [aws_s3_bucket.exports.arn, "${aws_s3_bucket.exports.arn}/*", aws_s3_bucket.media.arn, "${aws_s3_bucket.media.arn}/*"]
    }]
  })
}

locals {
  secret_keys = ["DJANGO_SECRET_KEY", "DB_APP_PASSWORD", "DB_OWNER_PASSWORD", "BILLING_WEBHOOK_SECRET", "MESSAGE_GATEWAY_TOKEN"]
  app_secrets = [for k in local.secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }]
  app_env = [
    { name = "DJANGO_DEBUG", value = "false" },
    { name = "DJANGO_ALLOWED_HOSTS", value = var.domain_name },
    { name = "TRUST_X_FORWARDED_FOR", value = "true" },
    { name = "FRONTEND_BASE_URL", value = "https://${var.domain_name}" },
    { name = "DB_HOST", value = aws_db_instance.main.address },
    { name = "DB_NAME", value = "fuelstation" },
    # TLS to Redis is on; Celery/Django use rediss://
    { name = "REDIS_URL", value = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0?ssl_cert_reqs=required" },
    { name = "AWS_STORAGE_BUCKET_NAME", value = aws_s3_bucket.media.bucket },
    { name = "EXPORT_S3_BUCKET", value = aws_s3_bucket.exports.bucket },
    { name = "AWS_S3_REGION_NAME", value = var.region },
    { name = "MESSAGE_PROVIDER", value = "apps.insights.messaging.HttpGatewayProvider" },
  ]
}

# One definition per process type; same image, different command.
resource "aws_ecs_task_definition" "backend" {
  for_each                 = { web = ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"], worker = ["celery", "-A", "config", "worker", "-l", "info", "--concurrency", "2"], beat = ["celery", "-A", "config", "beat", "-l", "info"] }
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.key == "web" ? var.web_cpu : 256
  memory                   = each.key == "web" ? var.web_memory : 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name             = each.key
    image            = var.backend_image
    command          = each.value
    essential        = true
    portMappings     = each.key == "web" ? [{ containerPort = 8000 }] : []
    environment      = local.app_env
    secrets          = local.app_secrets
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = each.key } }
  }])
}

# One-off task: roles + database setup, then migrations as the OWNER role. Run before each deploy (see docs/runbook.md).
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name             = "migrate"
    image            = var.backend_image
    command          = ["sh", "-c", "python manage.py bootstrap_db && python manage.py migrate --database=owner"]
    essential        = true
    environment      = concat(local.app_env, [{ name = "DB_ADMIN_USER", value = "fs_admin" }])
    secrets          = concat(local.app_secrets, [{ name = "DB_ADMIN_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" }])
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "migrate" } }
  }])
}

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${local.name}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name             = "frontend"
    image            = var.frontend_image
    essential        = true
    portMappings     = [{ containerPort = 80 }]
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.app.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = "frontend" } }
  }])
}

resource "aws_ecs_service" "web" {
  name            = "${local.name}-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend["web"].arn
  desired_count   = var.web_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "web"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.https]
}
resource "aws_ecs_service" "frontend" {
  name            = "${local.name}-frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = var.web_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "frontend"
    container_port   = 80
  }
  depends_on = [aws_lb_listener.https]
}
resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend["worker"].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
}
# Exactly ONE beat scheduler, otherwise jobs (reminders, summaries) would run twice.
resource "aws_ecs_service" "beat" {
  name                               = "${local.name}-beat"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.backend["beat"].arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
}
