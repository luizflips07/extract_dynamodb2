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
  name       = "${local.cfg.project}-${var.source_name}-writer" # UMA Lambda/role por fonte (contrato com a stack datalake)
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

# ---------- Parâmetros da Lambda genérica: uma entrada por tabela da fonte ----------
locals {
  # tables.json é empacotado no zip: a Lambda descobre a tabela pelo eventSourceARN e lê o prefixo/partição daqui
  table_cfg = {
    for t in values(local.tables) : t.table_name => {
      raw_prefix   = "dynamodb/${var.source_name}/${t.table_name}"
      partition_by = try(t.partition_by, "")
    }
  }
  streaming = { for k, t in local.tables : k => merge(local.cfg.streaming, try(t.streaming, {})) }
  alarms    = { for k, t in local.tables : k => merge(local.cfg.alarms, try(t.alarms, {})) }
  lambda    = local.cfg.lambda # a função é única: memória/timeout/concorrência valem para todas as tabelas
}

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/build/${local.prefix}.zip"

  source {
    content  = file("${path.module}/../../src/lambda/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${path.module}/../../src/lambda/partitioning.py")
    filename = "partitioning.py"
  }
  source {
    content  = jsonencode(local.table_cfg)
    filename = "tables.json"
  }
}

data "aws_dynamodb_table" "this" {
  for_each = local.tables
  name     = each.value.table_name
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

# ---------- Fila on-failure (uma por tabela: isola o reprocesso) ----------
resource "aws_sqs_queue" "on_failure" {
  for_each                   = local.tables
  name                       = "${local.prefix}-${each.value.table_name}-on-failure"
  kms_master_key_id          = aws_kms_key.app.id
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 300
}

resource "aws_sqs_queue_policy" "on_failure" {
  for_each  = local.tables
  queue_url = aws_sqs_queue.on_failure[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.on_failure[each.key].arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ---------- IAM da Lambda única: acesso restrito às tabelas/prefixos DESTA fonte ----------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "writer" {
  name               = local.name # contrato com a stack datalake: <project>-<source>-writer
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "writer" {
  statement {
    sid       = "ReadStreams"
    actions   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
    resources = [for d in data.aws_dynamodb_table.this : d.stream_arn]
  }
  statement {
    sid       = "WriteRaw"
    actions   = ["s3:PutObject"]
    resources = [for c in values(local.table_cfg) : "arn:${data.aws_partition.current.partition}:s3:::${local.cfg.datalake.raw_bucket}/${c.raw_prefix}/*"]
  }
  statement {
    sid       = "EncryptRaw"
    actions   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
    resources = [local.cfg.datalake.raw_kms_key_arn]
  }
  statement {
    sid       = "OnFailureDestinations"
    actions   = ["sqs:SendMessage"]
    resources = [for q in aws_sqs_queue.on_failure : q.arn]
  }
  statement {
    sid       = "AppKey"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.app.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.writer.arn}:*"]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "writer" {
  name   = "writer"
  role   = aws_iam_role.writer.id
  policy = data.aws_iam_policy_document.writer.json
}

# ---------- A Lambda genérica ----------
resource "aws_cloudwatch_log_group" "writer" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = local.lambda.log_retention_days
  kms_key_id        = aws_kms_key.app.arn
}

resource "aws_lambda_function" "writer" {
  function_name    = local.name
  role             = aws_iam_role.writer.arn
  handler          = "handler.handler"
  runtime          = local.lambda.runtime
  architectures    = [local.lambda.architecture]
  memory_size      = local.lambda.memory_mb
  timeout          = local.lambda.timeout_seconds
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  kms_key_arn      = aws_kms_key.app.arn

  reserved_concurrent_executions = local.lambda.reserved_concurrency

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      RAW_BUCKET      = local.cfg.datalake.raw_bucket
      RAW_KMS_KEY_ARN = local.cfg.datalake.raw_kms_key_arn
      SOURCE_NAME     = var.source_name
      LOG_LEVEL       = "INFO"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.writer.name
  }

  depends_on = [aws_iam_role_policy.writer]

  lifecycle {
    precondition {
      condition     = length(local.tables) > 0
      error_message = "Nenhuma tabela em tables/*.yaml pertence à fonte ${var.source_name}."
    }
    precondition {
      condition     = alltrue([for d in data.aws_dynamodb_table.this : d.stream_enabled && d.stream_view_type == "NEW_AND_OLD_IMAGES"])
      error_message = "Todas as tabelas precisam ter DynamoDB Streams com NEW_AND_OLD_IMAGES."
    }
    precondition {
      condition     = alltrue([for t in values(local.tables) : try(t.lambda, null) == null])
      error_message = "A Lambda é única por fonte: remova o bloco `lambda:` das tabelas e ajuste `lambda:` no config.yaml."
    }
  }
}

# ---------- Um Event Source Mapping por tabela (cada stream com seus parâmetros) ----------
resource "aws_lambda_event_source_mapping" "stream" {
  for_each                           = local.tables
  event_source_arn                   = data.aws_dynamodb_table.this[each.key].stream_arn
  function_name                      = aws_lambda_function.writer.arn
  starting_position                  = "TRIM_HORIZON"
  batch_size                         = local.streaming[each.key].batch_size
  maximum_batching_window_in_seconds = local.streaming[each.key].max_batching_window_seconds
  parallelization_factor             = local.streaming[each.key].parallelization_factor
  maximum_retry_attempts             = local.streaming[each.key].max_retry_attempts
  maximum_record_age_in_seconds      = local.streaming[each.key].max_record_age_seconds
  bisect_batch_on_function_error     = local.streaming[each.key].bisect_batch_on_error
  function_response_types            = ["ReportBatchItemFailures"]

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.on_failure[each.key].arn
    }
  }
}

# ---------- Alarmes ----------
# Da função (métricas AWS/Lambda, agregadas de todas as tabelas)
locals {
  function_alarms = {
    iterator-age = { metric = "IteratorAge", stat = "Maximum", threshold = local.cfg.alarms.iterator_age_ms, op = "GreaterThanThreshold", desc = "Atraso de leitura de algum stream. Perto de 24h = perda de dados." }
    errors       = { metric = "Errors", stat = "Sum", threshold = local.cfg.alarms.errors_threshold, op = "GreaterThanOrEqualToThreshold", desc = "Erros na Lambda writer." }
    throttles    = { metric = "Throttles", stat = "Sum", threshold = local.cfg.alarms.throttles_threshold, op = "GreaterThanOrEqualToThreshold", desc = "Throttling da Lambda." }
    duration-p99 = { metric = "Duration", stat = "p99", threshold = local.cfg.alarms.duration_p99_ms, op = "GreaterThanThreshold", desc = "Duração próxima do timeout." }
  }
}

resource "aws_cloudwatch_metric_alarm" "function" {
  for_each            = local.function_alarms
  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.desc
  namespace           = "AWS/Lambda"
  metric_name         = each.value.metric
  dimensions          = { FunctionName = aws_lambda_function.writer.function_name }
  statistic           = each.value.stat == "p99" ? null : each.value.stat
  extended_statistic  = each.value.stat == "p99" ? "p99" : null
  period              = 300
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = each.value.op
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

# Por tabela (métricas EMF emitidas pela Lambda, dimensão Table)
resource "aws_cloudwatch_metric_alarm" "stream_lag" {
  for_each            = local.tables
  alarm_name          = "${local.prefix}-${each.value.table_name}-stream-lag"
  alarm_description   = "Atraso (ms) entre o evento no stream e a gravação no RAW, desta tabela."
  namespace           = "DynamoRawIngest"
  metric_name         = "StreamLagMs"
  dimensions          = { Table = each.value.table_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.alarms[each.key].iterator_age_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "batch_failed" {
  for_each            = local.tables
  alarm_name          = "${local.prefix}-${each.value.table_name}-batch-failed"
  alarm_description   = "Lotes desta tabela que falharam ao gravar no RAW."
  namespace           = "DynamoRawIngest"
  metric_name         = "BatchFailed"
  dimensions          = { Table = each.value.table_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.alarms[each.key].errors_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "on_failure_depth" {
  for_each            = local.tables
  alarm_name          = "${local.prefix}-${each.value.table_name}-on-failure-not-empty"
  alarm_description   = "Lotes esgotaram retries e foram para a SQS on-failure."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.on_failure[each.key].name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

# ---------- Dashboard consolidado ----------
resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = local.prefix
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title   = "Atraso do stream por tabela (ms, max)", region = local.region, period = 300, stat = "Maximum"
          metrics = [for t in values(local.tables) : ["DynamoRawIngest", "StreamLagMs", "Table", t.table_name]]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Lotes com falha por tabela", region = local.region, period = 300, stat = "Sum"
          metrics = [for t in values(local.tables) : ["DynamoRawIngest", "BatchFailed", "Table", t.table_name]]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title   = "Registros gravados no RAW por tabela", region = local.region, period = 300, stat = "Sum"
          metrics = [for t in values(local.tables) : ["DynamoRawIngest", "RecordsWritten", "Table", t.table_name]]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "Lambda genérica: IteratorAge / Errors / Duration p99", region = local.region, period = 300
          metrics = [
            ["AWS/Lambda", "IteratorAge", "FunctionName", local.name, { stat = "Maximum" }],
            ["AWS/Lambda", "Errors", "FunctionName", local.name, { stat = "Sum" }],
            ["AWS/Lambda", "Duration", "FunctionName", local.name, { stat = "p99" }]
          ]
        }
      }
    ]
  })
}

# ---------- Outputs ----------
output "function_name" {
  description = "Lambda writer única desta fonte"
  value       = aws_lambda_function.writer.function_name
}

output "function_arn" {
  value = aws_lambda_function.writer.arn
}

output "writer_role_arn" {
  description = "Role da Lambda (a stack datalake libera bucket/CMK para este nome)"
  value       = aws_iam_role.writer.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.writer.name
}

output "alarm_topic_arn" {
  value = aws_sns_topic.alarms.arn
}

output "tables" {
  description = "Por tabela: stream, ESM, fila on-failure, destino no RAW e alarmes"
  value = {
    for k, t in local.tables : k => {
      table_name                = t.table_name
      stream_arn                = data.aws_dynamodb_table.this[k].stream_arn
      event_source_mapping_uuid = aws_lambda_event_source_mapping.stream[k].uuid
      on_failure_queue_url      = aws_sqs_queue.on_failure[k].id
      raw_s3_prefix             = "s3://${local.cfg.datalake.raw_bucket}/${local.table_cfg[t.table_name].raw_prefix}/"
      partition_by              = local.table_cfg[t.table_name].partition_by
      alarms = [
        aws_cloudwatch_metric_alarm.stream_lag[k].alarm_name,
        aws_cloudwatch_metric_alarm.batch_failed[k].alarm_name,
        aws_cloudwatch_metric_alarm.on_failure_depth[k].alarm_name,
      ]
    }
  }
}

output "tables_config" {
  description = "Parâmetros (tables.json) enviados à Lambda genérica"
  value       = local.table_cfg
}
