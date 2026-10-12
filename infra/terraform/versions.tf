terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.60" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Separate state per environment: terraform init -backend-config=envs/<env>.backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "fuelstation", Environment = var.environment, ManagedBy = "terraform" } }
}

# Second region for cross-region copies of database backups
provider "aws" {
  alias  = "backup"
  region = var.backup_region
}
