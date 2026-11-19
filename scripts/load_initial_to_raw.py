"""Carrega o snapshot da carga inicial (export PITR) nas tabelas RAW do Glue.

O export fica em  dynamodb/<fonte>/<tabela>-initial/AWSDynamoDB/<exportId>/data/*.json.gz
(formato DYNAMODB_JSON: {"Item": {...}}), fora do prefixo lido pela tabela Glue.
Este script converte cada linha para o MESMO envelope do CDC e grava em
  dynamodb/<fonte>/<tabela>/[<coluna>=<valor>/]dt=<data do export>/hh=<hora>/snapshot-<exportId>-<arquivo>.json.gz

`<coluna>=<valor>` só existe se a tabela declarar `partition_by` (mesma regra da Lambda).

Regras do envelope do snapshot:
  eventName = "SNAPSHOT"      (distingue da captura contínua)
  SequenceNumber = "0"        (perde para QUALQUER evento real do CDC na deduplicação da Trusted)
  NewImage = Item, Keys = atributos de `keys` do tables/<tabela>.yaml, OldImage = null

Idempotente: chave determinística (reexecutar sobrescreve os mesmos objetos).
Uso: python scripts/load_initial_to_raw.py --all [--source local] | --table clientes [--export-id ID]
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "lambda"))
import partitioning  # noqa: E402  (mesmo módulo usado pela Lambda)


def to_snapshot_groups(raw_lines, table: dict, export_id: str, part: str, export_dt: datetime,
                       ingested_at: str) -> dict[str | None, list[str]]:
    """Converte linhas do export em registros do envelope, agrupados pelo valor da partição."""
    column = table.get("partition_by")
    groups: dict[str | None, list[str]] = {}
    epoch = int(export_dt.timestamp())
    for n, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)["Item"]
        rec = {
            "eventID": f"snapshot-{export_id}-{part}-{n}",
            "eventName": "SNAPSHOT",
            "Keys": {k: item[k] for k in table["keys"]},
            "NewImage": item,
            "OldImage": None,
            "SequenceNumber": "0",
            "ApproximateCreationDateTime": epoch,
            "SizeBytes": len(line),
            "_source": table["source"],
            "_table": table["table_name"],
            "_ingested_at": ingested_at,
        }
        value = partitioning.partition_value(item, None, column) if column else None
        groups.setdefault(value, []).append(json.dumps(rec, separators=(",", ":")))
    return groups


def snapshot_key(table: dict, export_id: str, part: str, export_dt: datetime, part_value: str | None = None) -> str:
    column = table.get("partition_by")
    pdir = partitioning.partition_dir(column, part_value) if column and part_value is not None else ""
    return (f"dynamodb/{table['source']}/{table['table_name']}/{pdir}"
            f"dt={export_dt:%Y-%m-%d}/hh={export_dt:%H}/snapshot-{export_id}-{part}.json.gz")


def latest_export_id(s3, bucket: str, base: str) -> str:
    ids = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base + "/AWSDynamoDB/"):
        for o in page.get("Contents", []):
            m = re.search(r"/AWSDynamoDB/([^/]+)/manifest-summary\.json$", o["Key"])
            if m:
                ids.add(m.group(1))
    if not ids:
        raise SystemExit(f"nenhum export concluído em s3://{bucket}/{base}/")
    return sorted(ids)[-1]  # ids começam com timestamp: o maior é o mais recente


def load_table(s3, cfg: dict, table: dict, export_id: str | None) -> int:
    bucket = cfg["datalake"]["raw_bucket"]
    kms = cfg["datalake"]["raw_kms_key_arn"]
    base = f"dynamodb/{table['source']}/{table['table_name']}-initial"
    export_id = export_id or latest_export_id(s3, bucket, base)
    root = f"{base}/AWSDynamoDB/{export_id}"

    summary = json.loads(s3.get_object(Bucket=bucket, Key=f"{root}/manifest-summary.json")["Body"].read())
    export_dt = datetime.fromisoformat(summary["exportTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
    ingested_at = datetime.now(timezone.utc).isoformat()

    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{root}/data/"):
        for o in page.get("Contents", []):
            part = Path(o["Key"]).name.removesuffix(".json.gz")
            body = s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()
            groups = to_snapshot_groups(gzip.decompress(body).decode("utf-8").splitlines(),
                                        table, export_id, part, export_dt, ingested_at)
            for value, lines in groups.items():
                s3.put_object(
                    Bucket=bucket,
                    Key=snapshot_key(table, export_id, part, export_dt, value),
                    Body=gzip.compress(("\n".join(lines) + "\n").encode("utf-8")),
                    ContentType="application/x-ndjson",
                    ContentEncoding="gzip",
                    ServerSideEncryption="aws:kms",
                    SSEKMSKeyId=kms,
                    BucketKeyEnabled=True,
                )
                total += len(lines)
    print(f"{table['table_name']}: {total} itens do export {export_id} -> RAW")
    return total


def main() -> int:
    import boto3

    ap = argparse.ArgumentParser()
    ap.add_argument("--table")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--source")
    ap.add_argument("--export-id")
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    if not a.all and not a.table:
        ap.error("informe --table ou --all")

    root = Path(a.root)
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    files = sorted((root / "tables").glob("*.yaml")) if a.all else [root / "tables" / f"{a.table}.yaml"]
    s3 = boto3.client("s3", region_name=cfg["region"])
    for f in files:
        t = yaml.safe_load(f.read_text(encoding="utf-8"))
        if a.source and t["source"] != a.source:
            continue
        load_table(s3, cfg, t, a.export_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
