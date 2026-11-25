terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Envelope KEK / CMK for FlowPilot credentials & S3 artifacts
resource "aws_kms_key" "flowpilot" {
  description             = "FlowPilot ${var.environment} credential & artifact encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "flowpilot" {
  name          = "alias/flowpilot-${var.environment}"
  target_key_id = aws_kms_key.flowpilot.key_id
}

# Encrypted S3 bucket for large workflow execution artifacts (kept out of Temporal history)
resource "aws_s3_bucket" "artifacts" {
  bucket = "flowpilot-artifacts-${var.environment}-${var.account_suffix}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.flowpilot.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

# Managed PostgreSQL 16 (supports pgvector & Row-Level Security)
resource "aws_db_instance" "postgres" {
  identifier              = "flowpilot-${var.environment}-pg"
  engine                  = "postgres"
  engine_version          = "16.4"
  instance_class          = var.db_instance_class
  allocated_storage       = var.db_allocated_storage_gb
  storage_encrypted       = true
  kms_key_id              = aws_kms_key.flowpilot.arn
  db_name                 = "flowpilot"
  username                = var.db_username
  password                = var.db_password
  backup_retention_period = 14
  multi_az                = var.environment == "production"
  skip_final_snapshot     = var.environment != "production"
  publicly_accessible     = false
}

# Managed Redis for rate limiting, caching, and SSE Streams pub-sub
resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "flowpilot-${var.environment}-redis"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
}

# ECS Cluster for API & isolated worker pools (workflow, integrations, agents, code, browser)
resource "aws_ecs_cluster" "main" {
  name = "flowpilot-${var.environment}"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}
