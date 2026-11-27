# Conexão OIDC GitHub Actions -> AWS. Aplicada À MÃO, uma vez por conta (o pipeline não a gerencia,
# para não poder alterar as próprias permissões). Ver docs/GITHUB_ACTIONS.md.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 5.60" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
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
  default     = null
  description = "Vazio = detecta: cria o provedor se não existir, senão reaproveita. true/false força."
}

variable "python_command" {
  type        = string
  default     = "python"
  description = "Executável do Python usado na detecção do provedor (no Linux/macOS: python3)"
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
  boundary_name = "gha-${local.project}-boundary"
  boundary_arn  = "${local.arn}:iam::${local.account}:policy/${local.boundary_name}"
  data_buckets  = [local.raw_bucket, "${local.raw_bucket}-access-logs"]

  # Mesmas keys usadas pelo deploy.yml no backend S3
  state_folder = trim(try(local.cfg.tfstate_prefix, ""), "/")
  state_prefix = local.state_folder == "" ? "" : "${local.state_folder}/"
  state_keys = merge(
    { for s in local.producer_sources : "producer-${s}" => "${local.state_prefix}producer/${s}.tfstate" },
    local.is_datalake ? { datalake = "${local.state_prefix}datalake/raw.tfstate" } : {}
  )

  subject = var.github_subject_prefix != "" ? var.github_subject_prefix : "repo:${var.github_repository}"

  # O provedor é único por conta: a tag diz qual state o criou
  oidc_owner   = "${var.state_bucket}/${var.github_repository}"
  create_oidc  = var.create_oidc_provider != null ? var.create_oidc_provider : data.external.oidc_provider[0].result.create == "true"
  provider_arn = local.create_oidc ? aws_iam_openid_connect_provider.github[0].arn : "${local.arn}:iam::${local.account}:oidc-provider/token.actions.githubusercontent.com"
}

# Só leitura: já existe o provedor do GitHub na conta? (exige AWS CLI e Python)
data "external" "oidc_provider" {
  count   = var.create_oidc_provider == null ? 1 : 0
  program = [var.python_command, "${path.module}/find_oidc_provider.py"]
  query = {
    owner   = local.oidc_owner
    project = local.project
  }
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
  count           = local.create_oidc ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"] # a AWS valida o certificado do GitHub por conta própria
  tags            = { oidc-stack = local.oidc_owner }
}

# ---------- Quem pode assumir: só este repositório, nos environments do deploy ----------
data "aws_iam_policy_document" "trust" {
  for_each = merge(
    { for s in local.producer_sources : "producer-${s}" => ["environment:producer-${s}"] },
    local.is_datalake ? { datalake = ["environment:datalake-prod"] } : {}
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

# ---------- Permissions boundary: teto de TODA role que o pipeline cria (Lambdas writer e registradora) ----------
data "aws_iam_policy_document" "boundary" {
  statement {
    sid       = "ReadStreams"
    actions   = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:ListStreams"]
    resources = ["${local.arn}:dynamodb:${local.here}:table/*/stream/*"]
  }
  statement {
    sid       = "ReadParameters"
    actions   = ["dynamodb:GetItem"]
    resources = ["${local.arn}:dynamodb:${local.here}:table/${local.project}-*"]
  }
  statement {
    sid       = "WriteRaw"
    actions   = ["s3:PutObject"]
    resources = ["${local.arn}:s3:::${local.raw_bucket}/dynamodb/*"]
  }
  statement {
    sid       = "EncryptOnly" # a chave RAW pode ser de outra conta; a política da chave também precisa permitir
    actions   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"]
    resources = ["*"]
  }
  statement {
    sid       = "DecryptProjectKeys"
    actions   = ["kms:Decrypt"]
    resources = ["${local.arn}:kms:*:*:key/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/project"
      values   = [local.project]
    }
  }
  statement {
    sid       = "OnFailureQueue"
    actions   = ["sqs:SendMessage"]
    resources = ["${local.arn}:sqs:${local.here}:${local.project}-*"]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.arn}:logs:${local.here}:log-group:/aws/lambda/${local.project}-*"]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }
  statement {
    sid     = "RegisterPartitions"
    actions = ["glue:GetTable", "glue:CreatePartition", "glue:BatchCreatePartition"]
    resources = [
      "${local.arn}:glue:${local.here}:catalog",
      "${local.arn}:glue:${local.here}:database/${local.glue_db}",
      "${local.arn}:glue:${local.here}:table/${local.glue_db}/*",
    ]
  }
}

# O nome começa com "gha-": nenhuma role do pipeline consegue alterar esta política
resource "aws_iam_policy" "boundary" {
  count       = local.is_datalake || length(local.producer_sources) > 0 ? 1 : 0
  name        = local.boundary_name
  description = "Teto das roles criadas pelo pipeline de ${local.project}"
  policy      = data.aws_iam_policy_document.boundary.json
}

# ---------- State do Terraform: cada role só alcança o próprio arquivo ----------
data "aws_iam_policy_document" "state" {
  for_each = local.state_keys

  statement {
    sid       = "StateList" # o init lista o bucket; só nomes de objetos, sem conteúdo
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["${local.arn}:s3:::${var.state_bucket}"]
  }
  statement {
    sid       = "StateObject"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${local.arn}:s3:::${var.state_bucket}/${each.value}"]
  }
  statement {
    sid       = "StateLock"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.arn}:s3:::${var.state_bucket}/${each.value}.tflock"]
  }
}

# ---------- KMS: cria chaves já marcadas com project=<project> e administra só essas ----------
data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "KmsCreateTagged"
    actions   = ["kms:CreateKey"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/project"
      values   = [local.project]
    }
  }
  statement {
    sid       = "KmsList"
    actions   = ["kms:ListAliases"]
    resources = ["*"]
  }
  statement {
    sid     = "KmsAliases" # o lado da chave é autorizado por KmsProjectKeys
    actions = ["kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias"]
    resources = [
      "${local.arn}:kms:${local.here}:alias/${local.project}-*",
      "${local.arn}:kms:${local.here}:alias/${local.raw_bucket}",
    ]
  }
  # TagResource só vale para chaves que JÁ têm a tag: não dá para "adotar" a chave de outro projeto
  statement {
    sid = "KmsProjectKeys"
    actions = [
      "kms:DescribeKey", "kms:GetKeyPolicy", "kms:PutKeyPolicy", "kms:GetKeyRotationStatus", "kms:EnableKeyRotation",
      "kms:UpdateKeyDescription", "kms:ListResourceTags", "kms:TagResource", "kms:UntagResource",
      "kms:EnableKey", "kms:ScheduleKeyDeletion", "kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias",
      "kms:CreateGrant", "kms:ListGrants", "kms:RetireGrant",
      "kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext",
      "kms:ReEncryptFrom", "kms:ReEncryptTo",
    ]
    resources = ["${local.arn}:kms:*:*:key/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/project"
      values   = [local.project]
    }
  }
}

# ---------- Roles, funções e logs <project>-* (comum às duas roles) ----------
data "aws_iam_policy_document" "lambda_roles" {
  # Criar a role ou mudar o que ela pode só é permitido se ela estiver presa ao boundary
  statement {
    sid = "ProjectRolesWithBoundary"
    actions = [
      "iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:UpdateAssumeRolePolicy",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = ["${local.arn}:iam::${local.account}:role/${local.project}-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }
  statement {
    sid = "ProjectRoles"
    actions = [
      "iam:GetRole", "iam:UpdateRole", "iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:GetRolePolicy",
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
    sid = "Lambda"
    actions = [
      "lambda:CreateFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:DeleteFunction",
      "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:ListVersionsByFunction",
      "lambda:GetFunctionCodeSigningConfig", "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
      "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource",
    ]
    resources = ["${local.arn}:lambda:${local.here}:function:${local.project}-*"]
  }
  statement {
    sid = "Logs"
    actions = [
      "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
      "logs:AssociateKmsKey", "logs:DisassociateKmsKey", "logs:ListTagsForResource", "logs:ListTagsLogGroup",
      "logs:TagResource", "logs:UntagResource", "logs:TagLogGroup", "logs:UntagLogGroup",
    ]
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
    sid = "RawAndLogsBuckets"
    actions = [
      "s3:CreateBucket", "s3:DeleteBucket", "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:GetBucket*",
      "s3:GetAccelerateConfiguration", "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration", "s3:PutBucketLogging", "s3:PutBucketNotification",
      "s3:PutBucketOwnershipControls", "s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketTagging", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration", "s3:PutLifecycleConfiguration",
    ]
    resources = [for b in local.data_buckets : "${local.arn}:s3:::${b}"]
  }
  statement {
    sid       = "RawAndLogsObjects" # sem DeleteObject: o pipeline não apaga dados
    actions   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = [for b in local.data_buckets : "${local.arn}:s3:::${b}/*"]
  }
  statement {
    sid = "Glue"
    actions = [
      "glue:GetDatabase", "glue:CreateDatabase", "glue:UpdateDatabase", "glue:DeleteDatabase",
      "glue:GetTable", "glue:GetTables", "glue:GetTableVersions", "glue:CreateTable", "glue:UpdateTable",
      "glue:DeleteTable", "glue:GetPartition", "glue:GetPartitions", "glue:BatchGetPartition", "glue:CreatePartition",
      "glue:BatchCreatePartition", "glue:GetPartitionIndexes", "glue:GetTags", "glue:TagResource", "glue:UntagResource",
    ]
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
    sid = "PartitionRegistrarRule"
    actions = [
      "events:PutRule", "events:DescribeRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets",
      "events:ListTargetsByRule", "events:ListTagsForResource", "events:TagResource", "events:UntagResource",
    ]
    resources = ["${local.arn}:events:${local.here}:rule/${local.project}-*"]
  }
  statement {
    sid       = "RegistrarPermissionRead"
    actions   = ["lambda:GetPolicy", "lambda:RemovePermission"]
    resources = ["${local.arn}:lambda:${local.here}:function:${local.project}-*"]
  }
  statement {
    sid       = "RegistrarInvokedOnlyByEventBridge"
    actions   = ["lambda:AddPermission"]
    resources = ["${local.arn}:lambda:${local.here}:function:${local.project}-*"]
    condition {
      test     = "StringEquals"
      variable = "lambda:Principal"
      values   = ["events.amazonaws.com"]
    }
  }
}

# ---------- Producer (por fonte) ----------
data "aws_iam_policy_document" "producer" {
  statement {
    sid = "ParameterTable"
    actions = [
      "dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:UpdateTable", "dynamodb:DeleteTable",
      "dynamodb:DescribeContinuousBackups", "dynamodb:UpdateContinuousBackups", "dynamodb:DescribeTimeToLive",
      "dynamodb:ListTagsOfResource", "dynamodb:TagResource", "dynamodb:UntagResource",
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
    ]
    resources = ["${local.arn}:dynamodb:${local.here}:table/${local.project}-*"]
  }
  # Tabelas de origem: só as marcadas com a tag project=<project> (exige ABAC do DynamoDB na conta)
  statement {
    sid = "TaggedSourceTablesReadAndExport"
    actions = [
      "dynamodb:DescribeTable", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeTimeToLive",
      "dynamodb:ListTagsOfResource", "dynamodb:ExportTableToPointInTime",
    ]
    resources = ["${local.arn}:dynamodb:${local.here}:table/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/project"
      values   = [local.project]
    }
  }
  statement {
    sid       = "ExportStatus" # o recurso "export" não tem tags
    actions   = ["dynamodb:DescribeExport"]
    resources = ["${local.arn}:dynamodb:${local.here}:table/*/export/*"]
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
    sid       = "EventSourceMappingsRead" # a AWS não permite restringir estas por recurso
    actions   = ["lambda:GetEventSourceMapping", "lambda:ListEventSourceMappings"]
    resources = ["*"]
  }
  statement {
    sid       = "EventSourceMappingsTags"
    actions   = ["lambda:ListTags", "lambda:TagResource", "lambda:UntagResource"]
    resources = ["${local.arn}:lambda:${local.here}:event-source-mapping:*"]
  }
  statement {
    sid = "OnFailureQueue"
    actions = [
      "sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:GetQueueUrl",
      "sqs:ListQueueTags", "sqs:TagQueue", "sqs:UntagQueue",
    ]
    resources = ["${local.arn}:sqs:${local.here}:${local.project}-*"]
  }
  statement {
    sid = "AlarmTopic"
    actions = [
      "sns:CreateTopic", "sns:DeleteTopic", "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:Subscribe",
      "sns:Unsubscribe", "sns:GetSubscriptionAttributes", "sns:ListSubscriptionsByTopic", "sns:ListTagsForResource",
      "sns:TagResource", "sns:UntagResource",
    ]
    resources = ["${local.arn}:sns:${local.here}:${local.project}-*"]
  }
  statement {
    sid = "AlarmsAndDashboard"
    actions = [
      "cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:ListTagsForResource",
      "cloudwatch:TagResource", "cloudwatch:UntagResource", "cloudwatch:PutDashboard", "cloudwatch:GetDashboard",
      "cloudwatch:DeleteDashboards",
    ]
    resources = [
      "${local.arn}:cloudwatch:${local.here}:alarm:${local.project}-*",
      "${local.arn}:cloudwatch::${local.account}:dashboard/${local.project}-*",
    ]
  }
  statement {
    sid       = "AlarmRead"
    actions   = ["cloudwatch:DescribeAlarms"]
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
  count = local.is_datalake ? 1 : 0
  source_policy_documents = [
    data.aws_iam_policy_document.state["datalake"].json,
    data.aws_iam_policy_document.kms.json,
    data.aws_iam_policy_document.lambda_roles.json,
    data.aws_iam_policy_document.datalake.json,
  ]
}

resource "aws_iam_role_policy" "datalake" {
  count  = local.is_datalake ? 1 : 0
  name   = "deploy"
  role   = aws_iam_role.datalake[0].id
  policy = data.aws_iam_policy_document.datalake_all[0].json
}

resource "aws_iam_role" "producer" {
  for_each             = local.producer_sources
  name                 = "gha-${local.project}-producer-${each.key}"
  assume_role_policy   = data.aws_iam_policy_document.trust["producer-${each.key}"].json
  max_session_duration = 3600 # o export inicial é aguardado dentro desta sessão
}

data "aws_iam_policy_document" "producer_all" {
  for_each = local.producer_sources
  source_policy_documents = [
    data.aws_iam_policy_document.state["producer-${each.key}"].json,
    data.aws_iam_policy_document.kms.json,
    data.aws_iam_policy_document.lambda_roles.json,
    data.aws_iam_policy_document.producer.json,
  ]
}

resource "aws_iam_role_policy" "producer" {
  for_each = local.producer_sources
  name     = "deploy"
  role     = aws_iam_role.producer[each.key].id
  policy   = data.aws_iam_policy_document.producer_all[each.key].json
}

output "oidc_provider" {
  description = "Se o provedor OIDC é mantido por esta stack ou reaproveitado da conta"
  value       = local.create_oidc ? "criado por esta stack" : "reaproveitado (já existia na conta)"
}

output "permissions_boundary_arn" {
  description = "Boundary que as stacks datalake e producer colocam nas roles que criam"
  value       = try(aws_iam_policy.boundary[0].arn, null)
}

output "datalake_role_arn" {
  description = "Secret DATALAKE_DEPLOY_ROLE_ARN do repositório"
  value       = try(aws_iam_role.datalake[0].arn, null)
}

output "producer_role_arns" {
  description = "Secret PRODUCER_DEPLOY_ROLE_ARN de cada environment producer-<fonte>"
  value       = { for s, r in aws_iam_role.producer : s => r.arn }
}
