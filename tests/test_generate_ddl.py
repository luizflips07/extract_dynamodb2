import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import generate_trusted_ddl as g  # noqa: E402


def _load(p):
    return yaml.safe_load(Path(p).read_text(encoding="utf-8"))


def test_clientes_view():
    sql = g.build_view_sql(_load(ROOT / "tables" / "clientes.yaml"))
    assert "CREATE OR REPLACE VIEW trusted.local_clientes" in sql
    assert "FROM raw.dynamodb_clientes" in sql
    assert "PARTITION BY json_extract_scalar(keys, '$[\"id\"][\"s\"]')" in sql
    assert "CAST(json_extract_scalar(newimage, '$[\"ativo\"][\"bool\"]') AS boolean) AS ativo" in sql
    assert "from_iso8601_timestamp" in sql
    assert "eventname <> 'REMOVE'" in sql


def test_example_with_composite_key_and_types():
    sql = g.build_view_sql(_load(ROOT / "tables" / "_examples" / "pedidos.yaml"))
    assert sql.count("json_extract_scalar(keys,") == 2  # chave composta
    assert "AS decimal(18,2)" in sql
    assert "json_format(json_extract(newimage" in sql


def test_all_deployed_tables_are_valid():
    for f in (ROOT / "tables").glob("*.yaml"):
        g.validate(_load(f))


def test_key_must_be_in_columns():
    t = {"source": "s", "table_name": "t", "keys": ["x"], "columns": [{"name": "a", "dynamo": "S"}]}
    with pytest.raises(ValueError):
        g.build_view_sql(t)
