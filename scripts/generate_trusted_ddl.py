"""Gera a view tipada (camada Trusted) de cada tabela a partir de tables/*.yaml.

O RAW é schema-agnóstico (envelope CDC com DynamoDB JSON). Cada tabela declara
`keys` e `columns`; este script gera um `CREATE OR REPLACE VIEW` Athena que:
  - extrai e converte os atributos (S/N/BOOL/JSON) do NewImage;
  - deduplica por chave mantendo o evento de maior SequenceNumber (estado atual);
  - descarta chaves cujo último evento é REMOVE.

Uso: python scripts/generate_trusted_ddl.py [--tables tables] [--out sql/trusted]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DYNAMO_TYPES = {"S", "N", "BOOL", "JSON"}
RESERVED = {"eventid", "eventname", "keys", "newimage", "oldimage", "sequencenumber",
            "approximatecreationdatetime", "sizebytes", "_source", "_table", "_ingested_at", "dt", "hh"}


def _path(col: str, dynamo: str | None) -> str:
    # O JsonSerDe (OpenX) da tabela RAW converte TODAS as chaves JSON para minúsculas,
    # inclusive dentro de NewImage/Keys ({"id":{"s":"..."}}). O path precisa ser minúsculo.
    return f"$[\"{col.lower()}\"]" + (f"[\"{dynamo.lower()}\"]" if dynamo else "")


def _expr(image: str, col: dict) -> str:
    name, dyn = col["name"], col["dynamo"]
    typ = col.get("type")
    if dyn == "JSON":
        return f"json_format(json_extract({image}, '{_path(name, None)}'))"
    raw = f"json_extract_scalar({image}, '{_path(name, dyn)}')"
    if dyn == "BOOL":
        return f"CAST({raw} AS boolean)"
    if dyn == "N":
        return f"CAST({raw} AS {typ or 'double'})"
    # S
    if typ == "timestamp":
        # views Athena/Glue não aceitam "timestamp with time zone": converter p/ timestamp (UTC)
        return f"CAST(from_iso8601_timestamp({raw}) AS timestamp)"
    if typ in (None, "string"):
        return raw
    return f"CAST({raw} AS {typ})"


def validate(table: dict) -> None:
    cols = table.get("columns") or []
    names = [c["name"] for c in cols]
    if not table.get("keys"):
        raise ValueError(f"{table['table_name']}: 'keys' é obrigatório")
    if len(set(names)) != len(names):
        raise ValueError(f"{table['table_name']}: colunas duplicadas")
    for c in cols:
        if not IDENT.match(c["name"]):
            raise ValueError(f"{table['table_name']}: nome de coluna inválido: {c['name']}")
        if c["dynamo"] not in DYNAMO_TYPES:
            raise ValueError(f"{table['table_name']}.{c['name']}: dynamo deve ser {sorted(DYNAMO_TYPES)}")
    for k in table["keys"]:
        if k not in names:
            raise ValueError(f"{table['table_name']}: chave '{k}' não está em columns")
    pb = table.get("partition_by")
    if pb:
        if pb not in names:
            raise ValueError(f"{table['table_name']}: partition_by '{pb}' precisa estar em columns")
        if pb.lower() in RESERVED:
            raise ValueError(f"{table['table_name']}: partition_by usa nome reservado ({pb})")
        if next(c for c in cols if c["name"] == pb)["dynamo"] not in ("S", "N", "BOOL"):
            raise ValueError(f"{table['table_name']}: partition_by deve ser S, N ou BOOL")



def build_view_sql(table: dict, raw_db: str = "raw", trusted_db: str = "trusted") -> str:
    validate(table)
    cols = {c["name"]: c for c in table["columns"]}
    glue = table.get("glue_table", f"dynamodb_{table['table_name']}")
    view = f"{table['source']}_{table['table_name']}".replace("-", "_")

    key_exprs = [_expr("keys", cols[k]) for k in table["keys"]]
    inner = ",\n    ".join(
        [f"{_expr('newimage', c)} AS {c['name']}" for c in table["columns"]]
        + [
            "eventname",
            "approximatecreationdatetime",
            "sequencenumber",
            "ROW_NUMBER() OVER (\n      PARTITION BY "
            + ", ".join(key_exprs)
            + "\n      ORDER BY CAST(sequencenumber AS decimal(38,0)) DESC, approximatecreationdatetime DESC\n    ) AS _rn",
        ]
    )
    outer = ",\n  ".join(
        [c["name"] for c in table["columns"]]
        + ["CAST(from_unixtime(approximatecreationdatetime) AS timestamp) AS _changed_at", "sequencenumber AS _sequence"]
    )
    return (
        f"-- Gerado por scripts/generate_trusted_ddl.py a partir de tables/{table['table_name']}.yaml\n"
        f"CREATE OR REPLACE VIEW {trusted_db}.{view} AS\n"
        f"SELECT\n  {outer}\n"
        f"FROM (\n  SELECT\n    {inner}\n  FROM {raw_db}.{glue}\n)\n"
        f"WHERE _rn = 1 AND eventname <> 'REMOVE';\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default="tables")
    ap.add_argument("--out", default="sql/trusted")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    dl = cfg.get("datalake", {})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(Path(a.tables).glob("*.yaml")):
        t = yaml.safe_load(f.read_text(encoding="utf-8"))
        sql = build_view_sql(t, dl.get("glue_database", "raw"), dl.get("trusted_database", "trusted"))
        target = out / f"{t['source']}__{t['table_name']}.sql"
        target.write_text(sql, encoding="utf-8")
        print(target)
        n += 1
    print(f"{n} view(s) gerada(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
