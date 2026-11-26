# Conexão OIDC GitHub Actions -> AWS. Aplicada À MÃO, uma vez por conta (o pipeline não a gerencia,
# para não poder alterar as próprias permissões). Ver docs/GITHUB_ACTIONS.md.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
  backend "s3" {}
}

variable "github_repository" {
  type        = string
  description = "dono/repositorio autorizado a assumir as roles"
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use o formato dono/repositorio (sem curingas)."
  }
}

variable "github_subject_prefix" {
  type        = string
  default     = ""
  description = "Início do `sub` do token quando o repositório usa subject imutável (com IDs). Vazio = repo:<dono>/<repositorio>"
  validation {
    condition     = var.github_subject_prefix == "" || can(regex("^repo:[A-Za-z0-9_.-]+@[0-9]+/[A-Za-z0-9_.-]+@[0-9]+$", var.github_subject_prefix))
    error_message = "Use repo:<dono>@<id>/<repositorio>@<id> (sem curingas)."
  }
}

variable "state_bucket" {
  type        = string
  description = "Bucket do state do Terraform (o mesmo de TFSTATE_BUCKET)"
}

variable "create_oidc_provider" {
  type        = bool
  default     = true
  description = "false se a conta já tem o provedor token.actions.githubusercontent.com"
}

locals {
  cfg = yamldecode(file("${path.module}/../../config.yaml"))
  sources = {
    for f in fileset("${path.module}/../../sources", "*.yaml") :
    trimsuffix(f, ".yaml") => yamldecode(file("${path.module}/../../sources/${f}"))
  }

  project   = local.cfg.project
  region    = local.cfg.region
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  arn       = "arn:${local.partition}"
  here      = "${local.region}:${local.account}"

  raw_bucket = local.cfg.datalake.raw_bucket
  glue_db    = try(local.cfg.datalake.glue_database, "raw")
  trusted_db = try(local.cfg.datalake.trusted_database, "trusted")

  # Só cria as roles que pertencem à conta em que se está aplicando
  is_datalake      = local.account == local.cfg.datalake.account_id
  producer_sources = toset([for k, s in local.sources : k if s.account_id == local.account])

  # Prefixo "gha-" de propósito: fica FORA do padrão <project>-* que as próprias roles podem alterar
  datalake_role = "gha-${local.project}-datalake"

  subject = var.github_subject_prefix != "" ? var.github_subject_prefix : "repo:${var.github_repository}"

  provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "${local.arn}:iam::${local.account}:oidc-provider/token.actions.githubusercontent.com"
}

provider "aws" {
  region = local.region
  default_tags {
    tags = merge(local.cfg.tags, { project = local.project, managed-by = "terraform", layer = "ci" })
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_oidc_provider ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"] # a AWS valida o certificado do GitHub por conta própria
}

# ---------- Quem pode assumir: só este repositório, na main ou nos environments do deploy ----------
data "aws_iam_policy_document" "trust" {
  for_each = merge(
    { for s in local.producer_sources : "producer-${s}" => ["environment:producer-${s}"] },
    # o job `load` não usa environment: o token dele vem identificado pela branch
    local.is_datalake ? { datalake = ["environment:datalake-prod", "ref:refs/heads/main"] } : {}
  )

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for s in each.value : "${local.subject}:${s}"]
    }
  }
}

# ---------- Permissões comuns: state do Terraform ----------
data "aws_iam_policy_document" "state" {
  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["${local.arn}:s3:::${var.state_bucket}"]
  }
  statement {
    sid       = "StateObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.arn}:s3:::${var.state_bucket}/*"]
  }
}

# ---------- KMS: cria chaves e administra só as marcadas com project=<project> ----------
data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "KmsCreateAndList"
    actions   = ["kms:CreateKey", "kms:ListAliases", "kms:ListKeys", "kms:TagResource"]
    resources = ["*"]
  }
  statement {
    sid       = "KmsAliases"
    actions   = ["kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias"]
    resources = ["${local.arn}:kms:${local.here}:alias/*", "${local.arn}:kms:${local.here}:key/*"]
  }
  statement {
    sid       = "KmsProjectKeys"
    actions   = ["kms:*"]
    resources = ["${local.arn}:kms:*:*:key/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/project"
      values   = [local.project]
    }
  }
}

# ---------- IAM: só roles <project>-* (as das Lambdas) ----------
data "aws_iam_policy_document" "lambda_roles" {
  statement {
    sid = "ProjectRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
      "iam:TagRole", "iam:UntagRole", "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
      "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
    ]
    resources = ["${local.arn}:iam::${local.account}:role/${local.project}-*"]
  }
  statement {
    sid       = "PassOnlyToLambda"
    actions   = ["iam:PassRole"]
    resources = ["${local.arn}:iam::${local.account}:role/${local.project}-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }
  statement {
    sid       = "Lambda"
    actions   = ["lambda:*"]
    resources = ["${local.arn}:lambda:${local.here}:function:${local.project}-*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:*"]
    resources = ["${local.arn}:logs:${local.here}:log-group:/aws/lambda/${local.project}-*"]
  }
  statement {
    sid       = "LogsList"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }
}

# ---------- Data Lake ----------
data "aws_iam_policy_document" "datalake" {
  statement {
    sid     = "RawAndLogsBuckets"
    actions = ["s3:*"]
    resources = flatten([
      for b in [local.raw_bucket, "${local.raw_bucket}-access-logs"] :
      ["${local.arn}:s3:::${b}", "${local.arn}:s3:::${b}/*"]
    ])
  }
  statement {
    sid     = "Glue"
    actions = ["glue:*"]
    resources = concat(
      ["${local.arn}:glue:${local.here}:catalog"],
      flatten([for db in [local.glue_db, local.trusted_db] : [
        "${local.arn}:glue:${local.here}:database/${db}",
        "${local.arn}:glue:${local.here}:table/${db}/*",
      ]])
    )
  }
  statement {
    sid       = "AthenaViewsAndRepair"
    actions   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:GetWorkGroup"]
    resources = ["${local.arn}:athena:${local.here}:workgroup/primary"]
  }
  statement {
    sid       = "PartitionRegistrarRule"
    actions   = ["events:*"]
    resources = ["${local.arn}:events:${local.here}:rule/${local.project}-*"]
  }
}

# ---------- Producer (por fonte) ----------
data "aws_iam_policy_document" "producer" {
  statement {
    sid       = "ParameterTable"
    actions   = ["dynamodb:*"]
    resources = ["${local.arn}:dynamodb:${local.here}:table/${local.project}-*"]
  }
  statement {
    sid = "SourceTablesReadAndExport"
    actions = [
      "dynamodb:DescribeTable", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTimeToLive",
      "dynamodb:ListTagsOfResource", "dynamodb:ExportTableToPointInTime", "dynamodb:DescribeExport",
      "dynamodb:ListExports",
    ]
    resources = ["${local.arn}:dynamodb:${local.here}:table/*"]
  }
  statement {
    sid       = "InitialExportToRaw"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${local.arn}:s3:::${local.raw_bucket}/*-initial/*"]
  }
  statement {
    sid       = "EventSourceMappings"
    actions   = ["lambda:CreateEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"]
    resources = ["*"]
    condition {
      test     = "ArnLike"
      variable = "lambda:FunctionArn"
      values   = ["${local.arn}:lambda:${local.here}:function:${local.project}-*"]
    }
  }
  statement {
    sid       = "EventSourceMappingsRead"
    actions   = ["lambda:GetEventSourceMapping", "lambda:ListEventSourceMappings", "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource"]
    resources = ["*"]
  }
  statement {
    sid     = "QueuesTopicsDashboards"
    actions = ["sqs:*", "sns:*", "cloudwatch:*"]
    resources = [
      "${local.arn}:sqs:${local.here}:${local.project}-*",
      "${local.arn}:sns:${local.here}:${local.project}-*",
      "${local.arn}:cloudwatch:${local.here}:alarm:${local.project}-*",
      "${local.arn}:cloudwatch::${local.account}:dashboard/${local.project}-*",
    ]
  }
  statement {
    sid       = "AlarmAndSubscriptionRead"
    actions   = ["cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource", "sns:GetSubscriptionAttributes", "sns:Unsubscribe"]
    resources = ["*"]
  }
}

# ---------- Roles ----------
resource "aws_iam_role" "datalake" {
  count                = local.is_datalake ? 1 : 0
  name                 = local.datalake_role
  assume_role_policy   = data.aws_iam_policy_document.trust["datalake"].json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "datalake_all" {
  source_policy_documents = [
    data.aws_iam_policy_document.state.json,
    data.aws_iam_policy_document.kms.json,
    data.aws_iam_policy_document.lambda_roles.json,
    data.aws_iam_policy_document.datalake.json,
  ]
}

resource "aws_iam_role_policy" "datalake" {
  count  = local.is_datalake ? 1 : 0
  name   = "deploy"
  role   = aws_iam_role.datalake[0].id
  policy = data.aws_iam_policy_document.datalake_all.json
}

resource "aws_iam_role" "producer" {
  for_each             = local.producer_sources
  name                 = "gha-${local.project}-producer-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.trust["producer-${each.key}"].json
  max_session_duration = 3600 # o export inicial é aguardado dentro desta sessão
}

data "aws_iam_policy_document" "producer_all" {
  source_policy_documents = [
    data.aws_iam_policy_document.state.json,
    data.aws_iam_policy_document.kms.json,
    data.aws_iam_policy_document.lambda_roles.json,
    data.aws_iam_policy_document.producer.json,
  ]
}

resource "aws_iam_role_policy" "producer" {
  for_each = local.producer_sources
  name     = "deploy"
  role     = aws_iam_role.producer[each.key].id
  policy   = data.aws_iam_policy_document.producer_all.json
}

output "datalake_role_arn" {
  description = "Secret DATALAKE_DEPLOY_ROLE_ARN do repositório"
  value       = try(aws_iam_role.datalake[0].arn, null)
}

output "producer_role_arns" {
  description = "Secret PRODUCER_DEPLOY_ROLE_ARN de cada environment producer-<fonte>"
  value       = { for s, r in aws_iam_role.producer : s => r.arn }
}
