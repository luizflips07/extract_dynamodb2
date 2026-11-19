terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.60" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
  backend "s3" {}
}

# Um state por FONTE (conta produtora). Ex.: -var source_name=local
variable "source_name" {
  type        = string
  description = "Nome da fonte (arquivo sources/<nome>.yaml)"
}

locals {
  cfg    = yamldecode(file("${path.module}/../../config.yaml"))
  source = yamldecode(file("${path.module}/../../sources/${var.source_name}.yaml"))

  # Todas as tabelas em tables/*.yaml que pertencem a esta fonte
  all_tables = {
    for f in fileset("${path.module}/../../tables", "*.yaml") :
    trimsuffix(f, ".yaml") => yamldecode(file("${path.module}/../../tables/${f}"))
  }
  tables = { for k, t in local.all_tables : k => t if t.source == var.source_name }

  prefix     = "${local.cfg.project}-${var.source_name}"
  account_id = data.aws_caller_identity.current.account_id
  region     = local.cfg.region
  tags = merge(local.cfg.tags, {
    project     = local.cfg.project
    environment = local.cfg.environment
    source      = var.source_name
    managed-by  = "terraform"
  })
}

provider "aws" {
  region              = local.cfg.region
  allowed_account_ids = [local.source.account_id] # guarda contra apply na conta errada
  default_tags { tags = local.tags }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../../src/lambda"
  output_path = "${path.module}/build/${local.prefix}.zip"
}

# ---------- KMS da conta produtora (logs, SQS, SNS, env vars) - compartilhada ----------
data "aws_iam_policy_document" "kms_app" {
  statement {
    sid       = "AdminAccount"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${local.account_id}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.current.partition}:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${local.prefix}-*"]
    }
  }
  statement {
    sid       = "CloudWatchAlarmsToSNS"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "app" {
  description             = "${local.prefix} - logs, SQS, SNS"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms_app.json
}

resource "aws_kms_alias" "app" {
  name          = "alias/${local.prefix}"
  target_key_id = aws_kms_key.app.key_id
}

# ---------- SNS de alarmes (um por fonte) ----------
resource "aws_sns_topic" "alarms" {
  name              = "${local.prefix}-alarms"
  kms_master_key_id = aws_kms_key.app.id
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(local.cfg.alarms.notification_emails)
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# ---------- Uma ingestão isolada por tabela ----------
module "table" {
  source   = "./modules/table_ingest"
  for_each = local.tables

  name_prefix    = local.prefix
  source_name    = var.source_name
  table          = each.value
  cfg            = local.cfg
  kms_app_arn    = aws_kms_key.app.arn
  sns_topic_arn  = aws_sns_topic.alarms.arn
  zip_path       = data.archive_file.lambda.output_path
  zip_hash       = data.archive_file.lambda.output_base64sha256
  account_id     = local.account_id
  raw_prefix_fmt = "dynamodb/${var.source_name}/${each.value.table_name}"
}

# ---------- Dashboard consolidado ----------
resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = local.prefix
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title   = "IteratorAge (max) por tabela", region = local.region, period = 300, stat = "Maximum"
          metrics = [for t in values(module.table) : ["AWS/Lambda", "IteratorAge", "FunctionName", t.function_name]]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Erros por tabela", region = local.region, period = 300, stat = "Sum"
          metrics = [for t in values(module.table) : ["AWS/Lambda", "Errors", "FunctionName", t.function_name]]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 24, height = 6
        properties = {
          title   = "Registros gravados no RAW", region = local.region, period = 300, stat = "Sum"
          metrics = [for t in values(local.tables) : ["DynamoRawIngest", "RecordsWritten", "Table", t.table_name]]
        }
      }
    ]
  })
}

output "writer_role_arns" {
  value = { for k, m in module.table : k => m.role_arn }
}

output "on_failure_queues" {
  value = { for k, m in module.table : k => m.on_failure_queue_url }
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}
