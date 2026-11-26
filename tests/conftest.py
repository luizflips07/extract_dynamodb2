"""Configuração compartilhada: a Lambda writer genérica lê a tabela de parâmetros (aqui, uma falsa)."""
import json
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["RAW_BUCKET"] = "bucket-raw"
os.environ["PARAMETERS_TABLE"] = "projeto-local-parametros"

TABLES = {
    "clientes": {"raw_prefix": "dynamodb/local/clientes", "partition_by": ""},
    "vendas": {"raw_prefix": "dynamodb/local/vendas", "partition_by": ""},
    "clientes_part": {"raw_prefix": "dynamodb/local/clientes_part", "partition_by": "uf"},
}


class FakeParameters:
    """Cliente DynamoDB mínimo: get_item sobre um dicionário; `error` simula indisponibilidade."""

    def __init__(self):
        self.items = {name: json.dumps(cfg) for name, cfg in TABLES.items()}
        self.error = None
        self.calls = []

    def get_item(self, TableName, Key):
        self.calls.append((TableName, Key["nome_tabela"]["S"]))
        if self.error:
            raise self.error
        value = self.items.get(Key["nome_tabela"]["S"])
        return {"Item": {"configuracao": {"S": value}}} if value else {}


@pytest.fixture(autouse=True)
def parameters(monkeypatch):
    fake = FakeParameters()
    module = sys.modules.get("handler")
    if module is not None and hasattr(module, "_CACHE"):
        monkeypatch.setattr(module, "_ddb", fake)
        module._CACHE.clear()
    return fake
