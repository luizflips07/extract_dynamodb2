"""Configuração compartilhada: a Lambda writer genérica lê TABLES_CONFIG (cache por processo)."""
import json
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["RAW_BUCKET"] = "bucket-raw"
os.environ["TABLES_CONFIG"] = json.dumps(
    {
        "clientes": {"raw_prefix": "dynamodb/local/clientes", "partition_by": ""},
        "vendas": {"raw_prefix": "dynamodb/local/vendas", "partition_by": ""},
        "clientes_part": {"raw_prefix": "dynamodb/local/clientes_part", "partition_by": "uf"},
    }
)
