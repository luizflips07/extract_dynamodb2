terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name_prefix" { type = string }
variable "source_name" { type = string }
variable "table" { type = any }
variable "cfg" { type = any }
variable "kms_app_arn" { type = string }
variable "sns_topic_arn" { type = string }
variable "zip_path" { type = string }
variable "zip_hash" { type = string }
variable "account_id" { type = string }
variable "raw_prefix_fmt" { type = string }

locals {
  name      = "${var.name_prefix}-${var.table.table_name}"
  role_name = "${local.name}-writer" # contrato com a stack datalake: <project>-<source>-<table>-writer
  streaming = merge(var.cfg.streaming, try(var.table.streaming, {}))
  lambda    = merge(var.cfg.lambda, try(var.table.lambda, {}))
  alarms    = merge(var.cfg.alarms, try(var.table.alarms, {}))
}

data "aws_partition" "current" {}

data "aws_dynamodb_table" "this" {
  name = var.table.table_name
}

# ---------- IAM: menor privilégio, por tabela ----------
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
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "writer" {
  name               = local.role_name
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "writer" {
  statement {
    sid       = "ReadStream"
    actions   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
    resources = [data.aws_dynamodb_table.this.stream_arn]
  }
  statement {
    sid       = "WriteRaw"
    actions   = ["s3:PutObject"]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${var.cfg.datalake.raw_bucket}/${var.raw_prefix_fmt}/*"]
  }
  statement {
    sid       = "EncryptRaw"
    actions   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
    resources = [var.cfg.datalake.raw_kms_key_arn]
  }
  statement {
    sid       = "OnFailureDestination"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.on_failure.arn]
  }
  statement {
    sid       = "AppKey"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_app_arn]
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

# ---------- Logs, fila, Lambda ----------
resource "aws_cloudwatch_log_group" "writer" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = local.lambda.log_retention_days
  kms_key_id        = var.kms_app_arn
}

resource "aws_sqs_queue" "on_failure" {
  name                       = "${local.name}-on-failure"
  kms_master_key_id          = var.kms_app_arn
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 300
}

resource "aws_sqs_queue_policy" "on_failure" {
  queue_url = aws_sqs_queue.on_failure.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = aws_sqs_queue.on_failure.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_lambda_function" "writer" {
  function_name    = local.name
  role             = aws_iam_role.writer.arn
  handler          = "handler.handler"
  runtime          = local.lambda.runtime
  architectures    = [local.lambda.architecture]
  memory_size      = local.lambda.memory_mb
  timeout          = local.lambda.timeout_seconds
  filename         = var.zip_path
  source_code_hash = var.zip_hash
  kms_key_arn      = var.kms_app_arn

  reserved_concurrent_executions = local.lambda.reserved_concurrency

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = merge({
      RAW_BUCKET      = var.cfg.datalake.raw_bucket
      RAW_PREFIX      = var.raw_prefix_fmt
      RAW_KMS_KEY_ARN = var.cfg.datalake.raw_kms_key_arn
      SOURCE_NAME     = var.source_name
      TABLE_NAME      = var.table.table_name
      LOG_LEVEL       = "INFO"
      },
      # Coluna de partição do RAW (opcional: só é definida quando a tabela declara partition_by)
      try(var.table.partition_by, "") == "" ? {} : { PARTITION_BY = var.table.partition_by }
    )
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.writer.name
  }

  depends_on = [aws_iam_role_policy.writer]

  lifecycle {
    precondition {
      condition     = data.aws_dynamodb_table.this.stream_enabled && data.aws_dynamodb_table.this.stream_view_type == "NEW_AND_OLD_IMAGES"
      error_message = "A tabela ${var.table.table_name} precisa ter DynamoDB Streams com NEW_AND_OLD_IMAGES."
    }
  }
}

resource "aws_lambda_event_source_mapping" "stream" {
  event_source_arn                   = data.aws_dynamodb_table.this.stream_arn
  function_name                      = aws_lambda_function.writer.arn
  starting_position                  = "TRIM_HORIZON"
  batch_size                         = local.streaming.batch_size
  maximum_batching_window_in_seconds = local.streaming.max_batching_window_seconds
  parallelization_factor             = local.streaming.parallelization_factor
  maximum_retry_attempts             = local.streaming.max_retry_attempts
  maximum_record_age_in_seconds      = local.streaming.max_record_age_seconds
  bisect_batch_on_function_error     = local.streaming.bisect_batch_on_error
  function_response_types            = ["ReportBatchItemFailures"]

  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.on_failure.arn
    }
  }
}

# ---------- Alarmes ----------
locals {
  alarms_def = {
    iterator-age = { metric = "IteratorAge", stat = "Maximum", threshold = local.alarms.iterator_age_ms, op = "GreaterThanThreshold", desc = "Atraso de leitura do stream. Perto de 24h = perda de dados." }
    errors       = { metric = "Errors", stat = "Sum", threshold = local.alarms.errors_threshold, op = "GreaterThanOrEqualToThreshold", desc = "Erros na Lambda writer." }
    throttles    = { metric = "Throttles", stat = "Sum", threshold = local.alarms.throttles_threshold, op = "GreaterThanOrEqualToThreshold", desc = "Throttling da Lambda." }
    duration-p99 = { metric = "Duration", stat = "p99", threshold = local.alarms.duration_p99_ms, op = "GreaterThanThreshold", desc = "Duração próxima do timeout." }
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda" {
  for_each            = local.alarms_def
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
  alarm_actions       = [var.sns_topic_arn]
  ok_actions          = [var.sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "on_failure_depth" {
  alarm_name          = "${local.name}-on-failure-not-empty"
  alarm_description   = "Lotes esgotaram retries e foram para a SQS on-failure."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.on_failure.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.sns_topic_arn]
}

output "role_arn" { value = aws_iam_role.writer.arn }
output "function_name" { value = aws_lambda_function.writer.function_name }
output "on_failure_queue_url" { value = aws_sqs_queue.on_failure.id }
