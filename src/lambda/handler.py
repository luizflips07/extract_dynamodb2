"""Lambda writer: DynamoDB Streams -> S3 RAW (NDJSON.gz).

Um lote do Event Source Mapping vira UM objeto no S3 (ou um por valor da coluna de
partição, se `PARTITION_BY` estiver definido).
- Idempotente: a chave é determinística (primeiro/último SequenceNumber do grupo),
  então um retry do mesmo lote sobrescreve os mesmos objetos (sem duplicar).
- Falha parcial: ReportBatchItemFailures. Em erro reportamos o primeiro SequenceNumber
  (o lote inteiro é reprocessado/bissectado pelo ESM; objetos já gravados são sobrescritos).
- Observabilidade: logs JSON estruturados + métricas EMF.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.config import Config

import partitioning

RAW_BUCKET = os.environ.get("RAW_BUCKET", "")
RAW_PREFIX = os.environ.get("RAW_PREFIX", "dynamodb/local/clientes")
KMS_KEY_ARN = os.environ.get("RAW_KMS_KEY_ARN", "")
SOURCE_NAME = os.environ.get("SOURCE_NAME", "local")
TABLE_NAME = os.environ.get("TABLE_NAME", "clientes")
PARTITION_BY = os.environ.get("PARTITION_BY", "")  # vazio = sem partição extra
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "DynamoRawIngest")

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3 = boto3.client(
    "s3",
    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}, connect_timeout=5, read_timeout=30),
)


def _log(level: int, msg: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"message": msg, **fields}, default=str))


def _emit_metric(name: str, value: float, unit: str = "Count") -> None:
    """Embedded Metric Format: vira métrica no CloudWatch sem chamada de API."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": METRIC_NAMESPACE,
                            "Dimensions": [["Table"]],
                            "Metrics": [{"Name": name, "Unit": unit}],
                        }
                    ],
                },
                "Table": TABLE_NAME,
                name: value,
            }
        )
    )


def _to_cdc(record: dict[str, Any]) -> dict[str, Any]:
    ddb = record["dynamodb"]
    return {
        "eventID": record.get("eventID"),
        "eventName": record.get("eventName"),  # INSERT | MODIFY | REMOVE
        "Keys": ddb.get("Keys"),
        "NewImage": ddb.get("NewImage"),
        "OldImage": ddb.get("OldImage"),
        "SequenceNumber": ddb.get("SequenceNumber"),
        "ApproximateCreationDateTime": ddb.get("ApproximateCreationDateTime"),
        "SizeBytes": ddb.get("SizeBytes"),
        "_source": SOURCE_NAME,
        "_table": TABLE_NAME,
    }


def build_key(records: list[dict[str, Any]], part_value: str | None = None) -> str:
    first = records[0]["dynamodb"]
    last = records[-1]["dynamodb"]
    ts = datetime.fromtimestamp(int(first["ApproximateCreationDateTime"]), tz=timezone.utc)
    part = partitioning.partition_dir(PARTITION_BY, part_value) if PARTITION_BY and part_value is not None else ""
    return (
        f"{RAW_PREFIX}/{part}dt={ts:%Y-%m-%d}/hh={ts:%H}/"
        f"{first['SequenceNumber']}-{last['SequenceNumber']}.json.gz"
    )


def group_records(records: list[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    """Agrupa por valor da coluna de partição (mantém a ordem). Sem partição: um único grupo."""
    if not PARTITION_BY:
        return {None: records}
    groups: dict[str | None, list[dict[str, Any]]] = {}
    for rec in records:
        ddb = rec["dynamodb"]
        value = partitioning.partition_value(ddb.get("NewImage"), ddb.get("OldImage"), PARTITION_BY)
        groups.setdefault(value, []).append(rec)
    return groups


def _serialize(records: list[dict[str, Any]]) -> bytes:
    ingested_at = datetime.now(timezone.utc).isoformat()
    lines = []
    for rec in records:
        cdc = _to_cdc(rec)
        cdc["_ingested_at"] = ingested_at
        lines.append(json.dumps(cdc, separators=(",", ":"), default=str))
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), compresslevel=6)


def _put(key: str, body: bytes) -> None:
    kwargs: dict[str, Any] = {
        "Bucket": RAW_BUCKET,
        "Key": key,
        "Body": body,
        "ContentType": "application/x-ndjson",
        "ContentEncoding": "gzip",
        "ServerSideEncryption": "aws:kms",
        "ChecksumAlgorithm": "SHA256",
        # Conta destino é dona do objeto (bucket policy exige bucket-owner-full-control)
        "ACL": "bucket-owner-full-control",
    }
    if KMS_KEY_ARN:
        kwargs["SSEKMSKeyId"] = KMS_KEY_ARN
        kwargs["BucketKeyEnabled"] = True
    _s3.put_object(**kwargs)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    records = event.get("Records") or []
    if not records:
        return {"batchItemFailures": []}

    started = time.time()
    written = total_bytes = 0
    try:
        for part_value, group in group_records(records).items():
            key = build_key(group, part_value)
            body = _serialize(group)
            _put(key, body)
            written += len(group)
            total_bytes += len(body)
            _log(logging.INFO, "batch_written", key=key, records=len(group), bytes=len(body))
    except Exception as exc:  # noqa: BLE001 - reportar qualquer falha ao ESM
        _log(logging.ERROR, "put_object_failed", records=len(records), written=written, error=str(exc))
        _emit_metric("BatchFailed", 1)
        first_seq = records[0]["dynamodb"]["SequenceNumber"]
        return {"batchItemFailures": [{"itemIdentifier": first_seq}]}

    _log(logging.INFO, "batch_done", records=written, bytes=total_bytes, ms=round((time.time() - started) * 1000))
    _emit_metric("RecordsWritten", written)
    _emit_metric("BytesWritten", total_bytes, "Bytes")
    return {"batchItemFailures": []}
