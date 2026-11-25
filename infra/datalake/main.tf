terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.60" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
  backend "s3" {}
}

locals {
  cfg = yamldecode(file("${path.module}/../../config.yaml"))

  # TODAS as fontes e tabelas (um state para o Data Lake inteiro)
  sources = {
    for f in fileset("${path.module}/../../sources", "*.yaml") :
    trimsuffix(f, ".yaml") => yamldecode(file("${path.module}/../../sources/${f}"))
  }

  tables = {
    for f in fileset("${path.module}/../../tables", "*.yaml") :
    trimsuffix(f, ".yaml") => merge(
      yamldecode(file("${path.module}/../../tables/${f}")),
      { id = trimsuffix(f, ".yaml") }
    )
  }

  # Derivados por tabela (contrato de nomes com a stack producer)
  table_info = {
    for k, t in local.tables : k => {
      source     = t.source
      table_name = t.table_name
      glue_table = try(t.glue_table, "dynamodb_${t.table_name}")
      # Particionamento opcional do RAW por coluna (nomes de partição Hive são minúsculos)
      partition_by = try(lower(t.partition_by), "")
      column_names = [for c in try(t.columns, []) : lower(c.name)]
      # Assinatura do formato da tabela (partition_by + keys + columns). Se mudar, a tabela Glue é
      # recriada. DEVE ficar idêntica a signature() em scripts/table_sync.py.
      signature = sha256(join("|", [
        try(t.partition_by, ""),
        join(",", t.keys),
        # yamldecode lê `N` (sem aspas) como booleano false (YAML 1.1): normaliza de volta para "N"
        join(",", [for c in t.columns : "${c.name}:${lookup({ "false" = "N", "true" = "Y" }, tostring(c.dynamo), tostring(c.dynamo))}:${try(c.type, "")}"]),
      ]))
      prefix   = "dynamodb/${t.source}/${t.table_name}"
      role_arn = "arn:aws:iam::${local.sources[t.source].account_id}:role/${local.cfg.project}-${t.source}-writer"
    }
  }

  bucket     = local.cfg.datalake.raw_bucket
  glue_db    = try(local.cfg.datalake.glue_database, "raw")
  account_id = data.aws_caller_identity.current.account_id
  tags = merge(local.cfg.tags, {
    project     = local.cfg.project
    environment = local.cfg.environment
    managed-by  = "terraform"
    layer       = "raw"
  })
}

provider "aws" {
  region              = local.cfg.region
  allowed_account_ids = [local.cfg.datalake.account_id]
  default_tags { tags = local.tags }
}

data "aws_caller_identity" "current" {}

output "raw_bucket" {
  value = aws_s3_bucket.raw.id
}

output "raw_kms_key_arn" {
  value = aws_kms_key.raw.arn
}

output "glue_tables" {
  value = { for k, t in local.table_info : k => "${local.glue_db}.${t.glue_table}" }
}
