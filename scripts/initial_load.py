"""Carga inicial: export FULL (PITR) de tabelas DynamoDB para o S3 RAW.

Uso:
  python scripts/initial_load.py --table clientes
  python scripts/initial_load.py --all --source local
  python scripts/initial_load.py --tables clientes vendas --wait     # usado pelo deploy (aguarda COMPLETED)

Lê config.yaml, sources/<fonte>.yaml e tables/<tabela>.yaml; grava em
s3://<raw_bucket>/dynamodb/<fonte>/<tabela>-initial/ com SSE-KMS (CMK do Data Lake).
Depois rode scripts/load_initial_to_raw.py para levar o snapshot às tabelas RAW.
"""
import argparse
import sys
import time
from pathlib import Path

import boto3
import yaml


def load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def export_table(cfg: dict, table: dict, ddb=None) -> str:
    ddb = ddb or boto3.client("dynamodb", region_name=cfg["region"])
    arn = ddb.describe_table(TableName=table["table_name"])["Table"]["TableArn"]
    dl = cfg["datalake"]
    resp = ddb.export_table_to_point_in_time(
        TableArn=arn,
        S3Bucket=dl["raw_bucket"],
        S3BucketOwner=str(dl["account_id"]),
        S3Prefix=f"dynamodb/{table['source']}/{table['table_name']}-initial",
        S3SseAlgorithm="KMS",
        S3SseKmsKeyId=dl["raw_kms_key_arn"],
        ExportFormat="DYNAMODB_JSON",
    )
    return resp["ExportDescription"]["ExportArn"]


def wait_export(cfg: dict, export_arn: str, timeout_s: int = 3600) -> None:
    ddb = boto3.client("dynamodb", region_name=cfg["region"])
    started = time.time()
    while True:
        status = ddb.describe_export(ExportArn=export_arn)["ExportDescription"]["ExportStatus"]
        if status == "COMPLETED":
            return
        if status == "FAILED" or time.time() - started > timeout_s:
            raise SystemExit(f"export {export_arn}: {status}")
        time.sleep(15)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", help="nome do arquivo em tables/ (sem .yaml)")
    ap.add_argument("--tables", nargs="+", help="vários nomes de arquivo em tables/")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--source", help="limita a uma fonte")
    ap.add_argument("--wait", action="store_true", help="aguarda os exports ficarem COMPLETED")
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    root = Path(a.root)
    cfg = load(root / "config.yaml")

    if a.all:
        files = sorted((root / "tables").glob("*.yaml"))
    elif a.tables or a.table:
        files = [root / "tables" / f"{n}.yaml" for n in (a.tables or [a.table])]
    else:
        ap.error("informe --table, --tables ou --all")

    exports = []
    for f in files:
        t = load(f)
        if a.source and t["source"] != a.source:
            continue
        s = load(root / "sources" / f"{t['source']}.yaml")
        if not s.get("initial_load", False):
            print(f"{t['table_name']}: initial_load=false na fonte, ignorando")
            continue
        arn = export_table(cfg, t)
        print(t["table_name"], arn)
        exports.append(arn)
    if a.wait:
        for arn in exports:
            wait_export(cfg, arn)
            print("COMPLETED", arn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
