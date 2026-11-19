"""Sincronização por tabela no deploy: só age quando tables/*.yaml é NOVO ou MUDOU de formato.

Formato = partition_by + keys + columns (assinatura SHA-256). A assinatura aplicada fica gravada na
tabela Glue (parâmetro `dynalambda.signature`) pelo Terraform, que também APAGA E RECRIA a tabela
quando a assinatura muda. Este script decide o que o pipeline precisa fazer ao redor:

  plan  (ANTES do terraform apply)  compara YAML x Glue e classifica cada tabela:
        new        tabela Glue ainda não existe
        changed    existe, mas a assinatura difere (será recriada)
        unchanged  igual -> NENHUMA ação
  post  (DEPOIS do apply)           só para new/changed: registra partições existentes (MSCK, se
        partition_by), carrega o snapshot da carga inicial no RAW e recria as views Trusted.

Os dados já gravados no S3 NÃO são apagados.

Uso:
  python scripts/table_sync.py plan [--out changes.json]   # escreve também GITHUB_OUTPUT
  python scripts/table_sync.py post --changes changes.json [--source <fonte>] [--steps repair,snapshot,views]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))


def signature(table: dict) -> str:
    """DEVE ser idêntica a `signature` em infra/datalake/main.tf."""
    cols = ",".join(f"{c['name']}:{c['dynamo']}:{c.get('type', '')}" for c in table["columns"])
    raw = "|".join([table.get("partition_by") or "", ",".join(table["keys"]), cols])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def glue_table_name(table: dict) -> str:
    return table.get("glue_table") or f"dynamodb_{table['table_name']}"


def classify(table: dict, existing: dict | None) -> str:
    if existing is None:
        return "new"
    current = (existing.get("Parameters") or {}).get("dynalambda.signature")
    return "unchanged" if current == signature(table) else "changed"


def load_tables(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / "tables").glob("*.yaml")):
        t = yaml.safe_load(f.read_text(encoding="utf-8"))
        t["_name"] = f.stem
        out.append(t)
    return out


def cmd_plan(a, cfg, root) -> int:
    import boto3

    glue = boto3.client("glue", region_name=cfg["region"])
    db = cfg["datalake"].get("glue_database", "raw")
    changes = []
    for t in load_tables(root):
        try:
            existing = glue.get_table(DatabaseName=db, Name=glue_table_name(t))["Table"]
        except glue.exceptions.EntityNotFoundException:
            existing = None
        status = classify(t, existing)
        print(f"{t['_name']:<20} {status}")
        if status != "unchanged":
            changes.append({"name": t["_name"], "source": t["source"], "status": status,
                            "glue_table": glue_table_name(t), "partitioned": bool(t.get("partition_by"))})
    payload = {"tables": changes, "sources": sorted({c["source"] for c in changes})}
    Path(a.out).write_text(json.dumps(payload), encoding="utf-8")
    print(f"{len(changes)} tabela(s) nova(s)/alterada(s)" if changes else "nenhuma mudança de formato: nada a fazer")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"changes={json.dumps(payload, separators=(',', ':'))}\n")
            fh.write(f"has_changes={'true' if changes else 'false'}\n")
            fh.write(f"sources={json.dumps(payload['sources'], separators=(',', ':'))}\n")
    return 0


def _athena(athena, sql: str, results: str) -> None:
    qid = athena.start_query_execution(QueryString=sql, ResultConfiguration={"OutputLocation": results})[
        "QueryExecutionId"]
    while True:
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if st["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if st["State"] != "SUCCEEDED":
        raise SystemExit(f"Athena {st['State']}: {st.get('StateChangeReason')}")


def cmd_post(a, cfg, root) -> int:
    import boto3

    import apply_trusted_views as views
    import load_initial_to_raw as snapshot

    plan = json.loads(Path(a.changes).read_text(encoding="utf-8-sig"))
    todo = [c for c in plan["tables"] if not a.source or c["source"] == a.source]
    if not todo:
        print("nada a fazer")
        return 0
    steps = set(a.steps.split(","))
    dl = cfg["datalake"]
    results = dl.get("athena_results") or f"s3://{dl['raw_bucket']}-access-logs/athena-results/"
    athena = boto3.client("athena", region_name=cfg["region"])
    s3 = boto3.client("s3", region_name=cfg["region"])
    tables = {t["_name"]: t for t in load_tables(root)}
    sources = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in (root / "sources").glob("*.yaml")}

    for c in todo:
        t = tables[c["name"]]
        print(f"== {c['name']} ({c['status']})")
        if "snapshot" in steps and sources[t["source"]].get("initial_load", False):
            snapshot.load_table(s3, cfg, t, None)
        if "repair" in steps and c["partitioned"]:
            # depois do snapshot: registra todas as partições já existentes no S3 (novo formato)
            _athena(athena, f"MSCK REPAIR TABLE {dl.get('glue_database', 'raw')}.{c['glue_table']}", results)
            print("   partições existentes registradas (MSCK REPAIR)")
        if "views" in steps:
            sql = views.g.build_view_sql(t, dl.get("glue_database", "raw"), dl.get("trusted_database", "trusted"))
            views.run(athena, sql.rstrip().rstrip(";"), results)
            print("   view Trusted recriada")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--out", default="changes.json")
    q = sub.add_parser("post")
    q.add_argument("--changes", required=True)
    q.add_argument("--source")
    q.add_argument("--steps", default="repair,snapshot,views")
    a = ap.parse_args()
    root = Path(a.root)
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    return {"plan": cmd_plan, "post": cmd_post}[a.cmd](a, cfg, root)


if __name__ == "__main__":
    sys.exit(main())
