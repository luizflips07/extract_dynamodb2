import sys
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mock" / "data"))

import generate as g  # noqa: E402


def test_counts_and_determinism():
    assert len(list(g.gen_clientes(500))) == 500
    assert len(list(g.gen_produtos(100))) == 100
    assert list(g.gen_clientes(5)) == list(g.gen_clientes(5))
    vendas = list(g.gen_vendas(1000, 500, 100))
    assert len(vendas) == 1000
    assert len({(v["cliente_id"], v["venda_id"]) for v in vendas}) == 1000  # chaves únicas


def test_references_are_valid():
    for v in g.gen_vendas(500, 50, 10):
        assert 1 <= int(v["cliente_id"][1:]) <= 50
        assert 1 <= int(v["produto_id"][1:]) <= 10
        assert isinstance(v["valor_total"], Decimal)


def test_items_match_table_yaml_schema():
    gens = {
        "clientes": next(g.gen_clientes(1)),
        "produtos": next(g.gen_produtos(1)),
        "estoque": next(g.gen_estoque(1)),
        "vendas": next(g.gen_vendas(1, 5, 5)),
    }
    for name, item in gens.items():
        t = yaml.safe_load((ROOT / "tables" / f"{name}.yaml").read_text(encoding="utf-8"))
        for col in t["columns"]:
            assert col["name"] in item, f"{name}.{col['name']} ausente no mock"
        for k in t["keys"]:
            assert k in item
