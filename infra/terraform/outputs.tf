output "alb_dns_name" { value = aws_lb.main.dns_name }
output "backend_repository" { value = aws_ecr_repository.backend.repository_url }
output "frontend_repository" { value = aws_ecr_repository.frontend.repository_url }
output "cluster" { value = aws_ecs_cluster.main.name }
output "migrate_task_definition" { value = aws_ecs_task_definition.migrate.family }
output "private_subnets" { value = aws_subnet.private[*].id }
output "app_security_group" { value = aws_security_group.app.id }
output "db_identifier" { value = aws_db_instance.main.identifier }
