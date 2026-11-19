import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import load_initial_to_raw as li  # noqa: E402

TABLE = {"source": "local", "table_name": "vendas", "keys": ["cliente_id", "venda_id"]}
DT = datetime(2026, 11, 19, 12, 30, tzinfo=timezone.utc)
ITEM = '{"Item":{"cliente_id":{"S":"C1"},"venda_id":{"S":"V1"},"uf":{"S":"São Paulo"},"valor_total":{"N":"10.5"}}}'


def test_snapshot_envelope():
    groups = li.to_snapshot_groups([ITEM, ""], TABLE, "EXP1", "part1", DT, "2026-11-19T13:00:00+00:00")
    assert list(groups) == [None]
    r = json.loads(groups[None][0])
    assert r["eventName"] == "SNAPSHOT" and r["SequenceNumber"] == "0" and r["OldImage"] is None
    assert r["Keys"] == {"cliente_id": {"S": "C1"}, "venda_id": {"S": "V1"}}
    assert r["NewImage"]["valor_total"] == {"N": "10.5"}
    assert r["ApproximateCreationDateTime"] == int(DT.timestamp())


def test_key_is_deterministic_and_partitioned_by_time():
    k = li.snapshot_key(TABLE, "EXP1", "part1", DT)
    assert k == "dynamodb/local/vendas/dt=2026-11-19/hh=12/snapshot-EXP1-part1.json.gz"


def test_partition_by_column():
    t = {**TABLE, "partition_by": "uf"}
    groups = li.to_snapshot_groups([ITEM], t, "EXP1", "p", DT, "x")
    assert list(groups) == ["S_o_Paulo"]  # valor normalizado: caracteres fora de [A-Za-z0-9._-] -> "_"
    k = li.snapshot_key(t, "EXP1", "p", DT, "S_o_Paulo")
    assert k == "dynamodb/local/vendas/uf=S_o_Paulo/dt=2026-11-19/hh=12/snapshot-EXP1-p.json.gz"
