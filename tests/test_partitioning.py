import gzip
import json
import os
import sys
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["RAW_BUCKET"] = "bucket-raw"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import handler  # noqa: E402
import partitioning as p  # noqa: E402


def _rec(seq, uf, name="INSERT", ts=1_700_000_000, old=False, table="clientes_part"):
    img = {"id": {"S": seq}, "uf": {"S": uf}} if uf is not None else {"id": {"S": seq}}
    d = {"Keys": {"id": {"S": seq}}, "SequenceNumber": seq, "ApproximateCreationDateTime": ts, "SizeBytes": 1}
    d["OldImage" if old else "NewImage"] = img
    arn = f"arn:aws:dynamodb:us-east-1:111111111111:table/{table}/stream/2026-01-01T00:00:00.000"
    return {"eventID": seq, "eventName": name, "eventSourceARN": arn, "dynamodb": d}


def test_normalize_and_values():
    assert p.normalize("São Paulo/SP") == "S_o_Paulo_SP"
    assert p.normalize("") == p.NULL_VALUE and p.normalize(None) == p.NULL_VALUE
    assert p.attr_value({"N": "12"}) == "12" and p.attr_value({"BOOL": True}) == "true"
    assert p.attr_value({"M": {}}) is None
    assert p.partition_dir("UF", "SP") == "uf=SP/"


def test_remove_uses_old_image():
    assert p.partition_value(None, {"uf": {"S": "RJ"}}, "uf") == "RJ"


def test_handler_writes_one_object_per_partition_value():
    recs = [_rec("1", "SP"), _rec("2", "RJ"), _rec("3", "SP"), _rec("4", "RJ", "REMOVE", old=True), _rec("5", None)]
    with mock.patch.object(handler, "_s3") as s3:
        out = handler.handler({"Records": recs}, None)
    assert out == {"batchItemFailures": []}
    calls = {c.kwargs["Key"]: c.kwargs["Body"] for c in s3.put_object.call_args_list}
    assert len(calls) == 3
    sp = [k for k in calls if "/uf=SP/dt=2023-11-14/hh=22/" in k]
    assert sp and sp[0].endswith("/1-3.json.gz")
    n = sum(len(gzip.decompress(b).decode().strip().split("\n")) for b in calls.values())
    assert n == 5  # nenhum evento perdido
    assert any("/uf=__NULL__/" in k for k in calls)
    rj = [b for k, b in calls.items() if "/uf=RJ/" in k][0]
    assert [json.loads(x)["eventName"] for x in gzip.decompress(rj).decode().split("\n") if x] == ["INSERT", "REMOVE"]


def test_without_partition_by_single_object_same_as_before():
    recs = [_rec("1", "SP", table="clientes"), _rec("2", "RJ", table="clientes")]
    with mock.patch.object(handler, "_s3") as s3:
        handler.handler({"Records": recs}, None)
    assert s3.put_object.call_count == 1
    assert "/uf=" not in s3.put_object.call_args.kwargs["Key"]
