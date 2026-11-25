import gzip
import json
import os
import sys
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["RAW_BUCKET"] = "bucket-raw"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import handler  # noqa: E402


def _arn(table: str) -> str:
    return f"arn:aws:dynamodb:us-east-1:111111111111:table/{table}/stream/2026-01-01T00:00:00.000"


def _rec(seq: str, name: str = "INSERT", ts: int = 1_700_000_000, table: str = "clientes"):
    return {
        "eventSourceARN": _arn(table),
        "eventID": f"id-{seq}",
        "eventName": name,
        "dynamodb": {
            "Keys": {"id": {"S": seq}},
            "NewImage": {"id": {"S": seq}, "nome": {"S": "x"}},
            "SequenceNumber": seq,
            "ApproximateCreationDateTime": ts,
            "SizeBytes": 10,
        },
    }


def test_key_is_deterministic():
    recs = [_rec("100"), _rec("101")]
    key = handler.build_key(recs, "dynamodb/local/clientes")
    assert key == handler.build_key(recs, "dynamodb/local/clientes")
    assert key.endswith("/100-101.json.gz")
    assert "dt=2023-11-14/hh=22/" in key


def test_success_writes_ndjson_gz():
    recs = [_rec("100"), _rec("101", "MODIFY")]
    with mock.patch.object(handler, "_s3") as s3:
        out = handler.handler({"Records": recs}, None)
    assert out == {"batchItemFailures": []}
    kw = s3.put_object.call_args.kwargs
    assert kw["ServerSideEncryption"] == "aws:kms"
    lines = gzip.decompress(kw["Body"]).decode().strip().split("\n")
    assert [json.loads(l)["eventName"] for l in lines] == ["INSERT", "MODIFY"]


def test_failure_reports_first_sequence():
    recs = [_rec("100"), _rec("101")]
    with mock.patch.object(handler, "_s3") as s3:
        s3.put_object.side_effect = RuntimeError("boom")
        out = handler.handler({"Records": recs}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "100"}]}


def test_empty_batch():
    assert handler.handler({"Records": []}, None) == {"batchItemFailures": []}


def test_one_function_routes_each_table_to_its_own_prefix():
    with mock.patch.object(handler, "_s3") as s3:
        handler.handler({"Records": [_rec("1", table="clientes")]}, None)
        handler.handler({"Records": [_rec("2", table="vendas")]}, None)
    keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
    assert keys[0].startswith("dynamodb/local/clientes/dt=")
    assert keys[1].startswith("dynamodb/local/vendas/dt=")
    body = gzip.decompress(s3.put_object.call_args_list[1].kwargs["Body"]).decode()
    assert json.loads(body.strip())["_table"] == "vendas"


def test_unconfigured_table_fails_batch_without_writing():
    with mock.patch.object(handler, "_s3") as s3:
        out = handler.handler({"Records": [_rec("7", table="desconhecida")]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "7"}]}
    s3.put_object.assert_not_called()


def test_table_from_arn():
    assert handler.table_from_arn(_arn("minha-tabela")) == "minha-tabela"
