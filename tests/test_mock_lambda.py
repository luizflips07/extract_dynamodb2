import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(ROOT / "mock" / "data"))

spec = importlib.util.spec_from_file_location("mock_lambda_handler", ROOT / "mock_lambda" / "src" / "handler.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def test_table_keys_match_mock_terraform_and_yaml():
    tf = (ROOT / "mock" / "terraform" / "main.tf").read_text(encoding="utf-8")
    for name, (hash_key, range_key) in h.TABLES.items():
        m = re.search(rf'{name}\s*=\s*{{ hash_key = "(\w+)", range_key = (null|"\w+") }}', tf)
        assert m, f"{name} ausente no mock/terraform"
        assert m.group(1) == hash_key
        assert m.group(2).strip('"') == (range_key or "null")
        y = yaml.safe_load((ROOT / "tables" / f"{name}.yaml").read_text(encoding="utf-8"))
        assert y["keys"] == [k for k in (hash_key, range_key) if k]


def test_items_have_declared_columns():
    c = h.counts_from({"counts": {"vendas": 3, "clientes": 5, "faturamento": 2}})
    assert c["estoque"] == c["produtos"]
    for name in h.ORDER:
        y = yaml.safe_load((ROOT / "tables" / f"{name}.yaml").read_text(encoding="utf-8"))
        item = next(iter(h.make_items(name, c, 42)))
        assert {col["name"] for col in y["columns"]} <= set(item)


def test_delete_requires_confirm_and_unknown_table_rejected():
    with pytest.raises(ValueError):
        h.handler({"action": "delete_tables", "tables": ["clientes"]}, None)
    with pytest.raises(ValueError):
        h.handler({"tables": ["nao_existe"]}, None)
    with pytest.raises(ValueError):
        h.handler({"action": "x"}, None)
