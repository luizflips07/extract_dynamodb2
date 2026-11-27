# Registra no Glue as partições <coluna>=<valor>/dt=/hh= de tabelas com partition_by,
# a cada novo arquivo no RAW (S3 -> EventBridge -> Lambda). Só existe se alguma tabela usar partition_by.

locals {
  registrar_enabled = length(local.partitioned_tables) > 0
  registrar_name    = "${local.cfg.project}-partition-registrar"
  registrar_tables = {
    for k, t in local.partitioned_tables : t.prefix => {
      database = local.glue_db
      table    = t.glue_table
      column   = t.partition_by
    }
  }
}

data "archive_file" "registrar" {
  type        = "zip"
  source_dir  = "${path.module}/../../src/registrar"
  output_path = "${path.module}/build/registrar.zip"
}

resource "aws_s3_bucket_notification" "raw" {
  count       = local.registrar_enabled ? 1 : 0
  bucket      = aws_s3_bucket.raw.id
  eventbridge = true
}

data "aws_iam_policy_document" "registrar_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "registrar" {
  count              = local.registrar_enabled ? 1 : 0
  name               = local.registrar_name
  assume_role_policy = data.aws_iam_policy_document.registrar_assume.json

  permissions_boundary = var.use_permissions_boundary ? "arn:${data.aws_partition.current.partition}:iam::${local.account_id}:policy/gha-${local.cfg.project}-boundary" : null
}

data "aws_iam_policy_document" "registrar" {
  count = local.registrar_enabled ? 1 : 0
  statement {
    actions = ["glue:GetTable", "glue:BatchCreatePartition", "glue:CreatePartition"]
    resources = concat(
      ["arn:aws:glue:${local.cfg.region}:${local.account_id}:catalog", "arn:aws:glue:${local.cfg.region}:${local.account_id}:database/${local.glue_db}"],
      [for t in values(local.partitioned_tables) : "arn:aws:glue:${local.cfg.region}:${local.account_id}:table/${local.glue_db}/${t.glue_table}"]
    )
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.registrar[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "registrar" {
  count  = local.registrar_enabled ? 1 : 0
  name   = "registrar"
  role   = aws_iam_role.registrar[0].id
  policy = data.aws_iam_policy_document.registrar[0].json
}

resource "aws_cloudwatch_log_group" "registrar" {
  count             = local.registrar_enabled ? 1 : 0
  name              = "/aws/lambda/${local.registrar_name}"
  retention_in_days = try(local.cfg.lambda.log_retention_days, 90)
}

resource "aws_lambda_function" "registrar" {
  count            = local.registrar_enabled ? 1 : 0
  function_name    = local.registrar_name
  role             = aws_iam_role.registrar[0].arn
  handler          = "handler.handler"
  runtime          = try(local.cfg.lambda.runtime, "python3.12")
  architectures    = [try(local.cfg.lambda.architecture, "arm64")]
  memory_size      = 256
  timeout          = 30
  filename         = data.archive_file.registrar.output_path
  source_code_hash = data.archive_file.registrar.output_base64sha256

  environment {
    variables = { TABLES = jsonencode(local.registrar_tables) }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.registrar[0].name
  }

  depends_on = [aws_iam_role_policy.registrar]
}

resource "aws_cloudwatch_event_rule" "partition_created" {
  count       = local.registrar_enabled ? 1 : 0
  name        = "${local.registrar_name}-s3-object-created"
  description = "Novo arquivo no RAW de tabela particionada por coluna"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [local.bucket] }
      object = { key = [for t in values(local.partitioned_tables) : { prefix = "${t.prefix}/" }] }
    }
  })
  depends_on = [aws_s3_bucket_notification.raw]
}

resource "aws_cloudwatch_event_target" "registrar" {
  count = local.registrar_enabled ? 1 : 0
  rule  = aws_cloudwatch_event_rule.partition_created[0].name
  arn   = aws_lambda_function.registrar[0].arn
}

resource "aws_lambda_permission" "registrar_events" {
  count         = local.registrar_enabled ? 1 : 0
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.registrar[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.partition_created[0].arn
}
