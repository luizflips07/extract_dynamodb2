import json
import os
import sys
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["TABLES"] = json.dumps({"dynamodb/local/clientes": {"database": "raw", "table": "dynamodb_clientes", "column": "uf"}})

import table_sync as ts  # noqa: E402

T = {"table_name": "x", "source": "s", "keys": ["id"], "partition_by": "uf",
     "columns": [{"name": "id", "dynamo": "S"}, {"name": "uf", "dynamo": "S"}, {"name": "v", "dynamo": "N", "type": "decimal(10,2)"}]}


def test_signature_format_matches_terraform_contract():
    import hashlib
    expected = hashlib.sha256("uf|id|id:S:,uf:S:,v:N:decimal(10,2)".encode()).hexdigest()
    assert ts.signature(T) == expected


def test_signature_changes_with_partition_keys_or_columns():
    base = ts.signature(T)
    assert ts.signature({**T, "partition_by": None}) != base
    assert ts.signature({**T, "keys": ["id", "uf"]}) != base
    assert ts.signature({**T, "columns": T["columns"] + [{"name": "n", "dynamo": "S"}]}) != base
    assert ts.signature({**T, "columns": [dict(c) for c in T["columns"]]}) == base  # igual => igual


def test_classify():
    assert ts.classify(T, None) == "new"
    assert ts.classify(T, {"Parameters": {"dynalambda.signature": ts.signature(T)}}) == "unchanged"
    assert ts.classify(T, {"Parameters": {"dynalambda.signature": "old"}}) == "changed"
    assert ts.classify(T, {"Parameters": {}}) == "changed"


def test_every_deployed_table_has_a_signature():
    for f in (ROOT / "tables").glob("*.yaml"):
        assert len(ts.signature(yaml.safe_load(f.read_text(encoding="utf-8")))) == 64


def test_registrar_parse_and_register():
    import importlib.util
    spec = importlib.util.spec_from_file_location("registrar_handler", ROOT / "src" / "registrar" / "handler.py")
    reg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reg)
    p = reg.parse_key("dynamodb/local/clientes/uf=SP/dt=2026-11-19/hh=12/1-2.json.gz")
    assert p and p[1:] == ("SP", "2026-11-19", "12")
    assert reg.parse_key("dynamodb/local/clientes/dt=2026-11-19/hh=12/1-2.json.gz") is None  # sem partição
    assert reg.parse_key("dynamodb/local/outra/uf=SP/dt=2026-11-19/hh=12/f.gz") is None

    glue = mock.Mock()
    glue.get_table.return_value = {"Table": {"StorageDescriptor": {"Location": "s3://b/dynamodb/local/clientes/"}}}
    glue.batch_create_partition.return_value = {}
    glue.exceptions.AlreadyExistsException = type("AlreadyExists", (Exception,), {})
    with mock.patch.object(reg, "_glue", glue):
        reg._table_cache.clear()
        out = reg.handler({"detail": {"object": {"key": "dynamodb/local/clientes/uf=ZZ/dt=2026-11-19/hh=12/f.json.gz"}}}, None)
    assert out == {"registered": 1, "ignored": 0}
    part = glue.batch_create_partition.call_args.kwargs["PartitionInputList"][0]
    assert part["Values"] == ["ZZ", "2026-11-19", "12"]
    assert part["StorageDescriptor"]["Location"] == "s3://b/dynamodb/local/clientes/uf=ZZ/dt=2026-11-19/hh=12/"
