# MOCK LAMBDA: implanta a Lambda que cria as tabelas DynamoDB de exemplo e carrega dados fictícios.
# Execução MANUAL (terraform init/apply nesta pasta). NÃO faz parte do GitHub Actions.
# State local de propósito: ambiente descartável. Não requer build: o zip é montado pelo archive_file
# com mock_lambda/src/handler.py + mock/data/generate.py.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.60" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "allowed_account_id" {
  type        = string
  default     = null
  description = "Opcional: trava o apply na conta produtora correta (recomendado)."
}

variable "function_name" {
  type    = string
  default = "mock-dynamodb-loader"
}

variable "table_name_prefix" {
  type        = string
  default     = ""
  description = "Limita a role da Lambda às tabelas com este prefixo (vazio = todas as tabelas da conta/região)."
}

variable "pipeline_project" {
  type        = string
  default     = "dynamodb-produtor"
  description = "`project` do config.yaml: vira a tag project das tabelas criadas"
}

variable "memory_mb" {
  type    = number
  default = 1024
}

variable "timeout_seconds" {
  type        = number
  default     = 900
  description = "Máximo da Lambda: 900 s. A carga de 100.000 vendas deve caber; senão invoque por partes (veja o README)."
}

provider "aws" {
  region              = var.region
  allowed_account_ids = var.allowed_account_id == null ? null : [var.allowed_account_id]
  default_tags {
    tags = { project = "dynamodb-produtor-mock", managed-by = "terraform-manual", purpose = "mock" }
  }
}

data "aws_caller_identity" "current" {}

data "archive_file" "lambda" {
  type        = "zip"
  output_path = "${path.module}/build/mock-lambda.zip"

  source {
    content  = file("${path.module}/../src/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${path.module}/../../mock/data/generate.py")
    filename = "generate.py"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 14
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "permissions" {
  statement {
    sid = "MockTables"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DescribeTable",
      "dynamodb:DeleteTable",
      "dynamodb:TagResource",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:DescribeContinuousBackups",
      "dynamodb:PutItem",
      "dynamodb:BatchWriteItem",
    ]
    resources = ["arn:aws:dynamodb:${var.region}:${data.aws_caller_identity.current.account_id}:table/${var.table_name_prefix}*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "mock-loader"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  role             = aws_iam_role.this.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  memory_size      = var.memory_mb
  timeout          = var.timeout_seconds
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = { PIPELINE_PROJECT = var.pipeline_project }
  }

  depends_on = [aws_cloudwatch_log_group.this, aws_iam_role_policy.this]
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "region" {
  value = var.region
}
