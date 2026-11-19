import gzip
import json
import os
import sys
from unittest import mock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["RAW_BUCKET"] = "bucket-raw"
os.environ["RAW_PREFIX"] = "dynamodb/local/clientes"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "lambda"))

import handler  # noqa: E402


def _rec(seq: str, name: str = "INSERT", ts: int = 1_700_000_000):
    return {
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
    assert handler.build_key(recs) == handler.build_key(recs)
    assert handler.build_key(recs).endswith("/100-101.json.gz")
    assert "dt=2023-11-14/hh=22/" in handler.build_key(recs)


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
