"""Gera e cria/atualiza no Athena as views Trusted de todas as tabelas (tables/*.yaml).

Uso: python scripts/apply_trusted_views.py [--results s3://bucket/prefix/]

Resultados das queries vão para `datalake.athena_results` (config.yaml) ou, por padrão,
s3://<raw_bucket>-access-logs/athena-results/ (bucket de logs, fora do RAW).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import generate_trusted_ddl as g  # noqa: E402


def run(athena, sql: str, results: str) -> None:
    qid = athena.start_query_execution(QueryString=sql, ResultConfiguration={"OutputLocation": results})[
        "QueryExecutionId"
    ]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1.5)
    if st["State"] != "SUCCEEDED":
        raise SystemExit(f"Athena {st['State']}: {st.get('StateChangeReason')}")


def main() -> int:
    import boto3

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--results")
    ap.add_argument("--tables", nargs="*", help="somente estas tabelas (nome do arquivo em tables/ sem .yaml)")
    a = ap.parse_args()
    root = Path(a.root)
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    dl = cfg["datalake"]
    results = a.results or dl.get("athena_results") or f"s3://{dl['raw_bucket']}-access-logs/athena-results/"
    athena = boto3.client("athena", region_name=cfg["region"])
    for f in sorted((root / "tables").glob("*.yaml")):
        if a.tables is not None and f.stem not in a.tables:
            continue
        t = yaml.safe_load(f.read_text(encoding="utf-8"))
        sql = g.build_view_sql(t, dl.get("glue_database", "raw"), dl.get("trusted_database", "trusted"))
        run(athena, sql.rstrip().rstrip(";"), results)
        print(f"view trusted.{t['source']}_{t['table_name']} atualizada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
