# MOCK: cria 4 tabelas DynamoDB de exemplo para testar o pipeline de CDC.
# Execução MANUAL (terraform init/apply nesta pasta). NÃO faz parte do GitHub Actions.
# State local (terraform.tfstate) de propósito: é um ambiente descartável.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
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

variable "table_name_prefix" {
  type        = string
  default     = ""
  description = "Prefixo opcional. Mantenha vazio para casar com tables/*.yaml do pipeline."
}

variable "deletion_protection" {
  type        = bool
  default     = false
  description = "false facilita o terraform destroy do mock."
}

provider "aws" {
  region              = var.region
  allowed_account_ids = var.allowed_account_id == null ? null : [var.allowed_account_id]
  default_tags {
    tags = { project = "dynamodb-produtor-mock", managed-by = "terraform-manual", purpose = "mock" }
  }
}

locals {
  # Chaves alinhadas com tables/*.yaml (campo `keys`)
  tables = {
    clientes = { hash_key = "id", range_key = null }
    produtos = { hash_key = "id", range_key = null }
    vendas   = { hash_key = "cliente_id", range_key = "venda_id" }
    estoque  = { hash_key = "produto_id", range_key = null }
  }
}

resource "aws_dynamodb_table" "this" {
  for_each = local.tables

  name         = "${var.table_name_prefix}${each.key}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value.hash_key
  range_key    = each.value.range_key

  # Pré-requisitos do pipeline
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  point_in_time_recovery {
    enabled = true # necessário para a carga inicial (export)
  }

  server_side_encryption {
    enabled = true # chave gerenciada aws/dynamodb
  }

  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = each.value.hash_key
    type = "S"
  }

  dynamic "attribute" {
    for_each = each.value.range_key == null ? [] : [each.value.range_key]
    content {
      name = attribute.value
      type = "S"
    }
  }
}

output "tables" {
  value = {
    for k, t in aws_dynamodb_table.this : k => {
      name       = t.name
      arn        = t.arn
      stream_arn = t.stream_arn
    }
  }
}
