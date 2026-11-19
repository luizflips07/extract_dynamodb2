locals {
  # Envelope CDC fixo: igual para toda tabela, independente do schema do item.
  # O schema tipado de cada tabela é derivado na camada Trusted (scripts/generate_trusted_ddl.py).
  envelope_columns = [
    { name = "eventid", type = "string" },
    { name = "eventname", type = "string" },
    { name = "keys", type = "string" },
    { name = "newimage", type = "string" },
    { name = "oldimage", type = "string" },
    { name = "sequencenumber", type = "string" },
    { name = "approximatecreationdatetime", type = "bigint" },
    { name = "sizebytes", type = "bigint" },
    { name = "_source", type = "string" },
    { name = "_table", type = "string" },
    { name = "_ingested_at", type = "string" },
  ]
  reserved_names = concat([for c in local.envelope_columns : c.name], ["dt", "hh"])

  # Tabelas particionadas por coluna (partition_by): partições registradas DINAMICAMENTE
  # (Lambda registrar + MSCK no deploy), pois os valores não são conhecidos de antemão.
  # Tabelas sem partition_by continuam com partition projection (dt/hh).
  partitioned_tables = { for k, t in local.table_info : k => t if t.partition_by != "" }

  projection_params = {
    "projection.enabled"   = "true"
    "projection.dt.type"   = "date"
    "projection.dt.format" = "yyyy-MM-dd"
    "projection.dt.range"  = "${try(local.cfg.datalake.partition_start_date, "2024-01-01")},NOW"
    "projection.hh.type"   = "integer"
    "projection.hh.range"  = "0,23"
    "projection.hh.digits" = "2"
  }
}

resource "aws_glue_catalog_database" "raw" {
  name = local.glue_db
}

resource "aws_glue_catalog_database" "trusted" {
  name = try(local.cfg.datalake.trusted_database, "trusted")
}

# Mudou partition_by/keys/columns => esta assinatura muda => a tabela Glue é APAGADA e RECRIADA
# (replace_triggered_by). Sem mudança, nada é feito. Os dados no S3 NÃO são apagados.
resource "terraform_data" "table_signature" {
  for_each = local.table_info
  input    = each.value.signature
}

resource "aws_glue_catalog_table" "cdc" {
  for_each      = local.table_info
  name          = each.value.glue_table
  database_name = aws_glue_catalog_database.raw.name
  table_type    = "EXTERNAL_TABLE"

  parameters = merge(
    {
      "classification"          = "json"
      "compressionType"         = "gzip"
      "dynalambda.signature"    = each.value.signature
      "dynalambda.partition_by" = each.value.partition_by
    },
    each.value.partition_by == "" ? merge(local.projection_params, {
      "storage.location.template" = "s3://${local.bucket}/${each.value.prefix}/dt=$${dt}/hh=$${hh}"
    }) : {}
  )

  # Ordem = ordem do caminho no S3: [<coluna>=…/]dt=…/hh=…
  dynamic "partition_keys" {
    for_each = concat(each.value.partition_by == "" ? [] : [each.value.partition_by], ["dt", "hh"])
    content {
      name = partition_keys.value
      type = "string"
    }
  }

  storage_descriptor {
    location      = "s3://${local.bucket}/${each.value.prefix}/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters            = { "ignore.malformed.json" = "true" }
    }

    dynamic "columns" {
      for_each = local.envelope_columns
      content {
        name = columns.value.name
        type = columns.value.type
      }
    }
  }

  lifecycle {
    replace_triggered_by = [terraform_data.table_signature[each.key]]

    precondition {
      condition     = each.value.partition_by == "" || contains(each.value.column_names, each.value.partition_by)
      error_message = "tables/${each.key}.yaml: partition_by '${each.value.partition_by}' precisa estar listada em columns."
    }
    precondition {
      condition     = !contains(local.reserved_names, each.value.partition_by)
      error_message = "tables/${each.key}.yaml: partition_by não pode usar nome reservado (${join(", ", local.reserved_names)})."
    }
    precondition {
      condition     = alltrue([for k in try(local.tables[each.key].keys, []) : contains(each.value.column_names, lower(k))])
      error_message = "tables/${each.key}.yaml: todas as keys precisam estar listadas em columns."
    }
  }
}
