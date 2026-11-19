"""Registrar de partições: cria no Glue a partição de cada novo arquivo do RAW.

Disparado pelo EventBridge (S3 "Object Created") para tabelas com `partition_by`.
Chave esperada:  <prefixo>/<coluna>=<valor>/dt=YYYY-MM-DD/hh=HH/<arquivo>
Cria a partição [<valor>, <dt>, <hh>] (idempotente: AlreadyExists é ignorado).
Variável TABLES = JSON {"<prefixo>": {"database": "raw", "table": "dynamodb_x", "column": "uf"}}.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLES: dict[str, dict[str, str]] = json.loads(os.environ.get("TABLES", "{}"))
_glue = boto3.client("glue")
_table_cache: dict[tuple[str, str], dict] = {}


def parse_key(key: str) -> tuple[dict[str, str], str, str, str] | None:
    """Devolve (config da tabela, valor, dt, hh) ou None se a chave não for de partição."""
    for prefix, cfg in TABLES.items():
        pat = re.compile(
            rf"^{re.escape(prefix)}/{re.escape(cfg['column'].lower())}=([^/]+)/dt=(\d{{4}}-\d{{2}}-\d{{2}})/hh=(\d{{2}})/[^/]+$"
        )
        m = pat.match(key)
        if m:
            return cfg, m.group(1), m.group(2), m.group(3)
    return None


def _table(db: str, name: str) -> dict:
    if (db, name) not in _table_cache:
        _table_cache[(db, name)] = _glue.get_table(DatabaseName=db, Name=name)["Table"]
    return _table_cache[(db, name)]


def register(cfg: dict[str, str], value: str, dt: str, hh: str) -> bool:
    table = _table(cfg["database"], cfg["table"])
    sd = copy.deepcopy(table["StorageDescriptor"])
    sd["Location"] = f"{sd['Location'].rstrip('/')}/{cfg['column'].lower()}={value}/dt={dt}/hh={hh}/"
    try:
        resp = _glue.batch_create_partition(
            DatabaseName=cfg["database"],
            TableName=cfg["table"],
            PartitionInputList=[{"Values": [value, dt, hh], "StorageDescriptor": sd}],
        )
    except _glue.exceptions.AlreadyExistsException:
        return False
    errors = [e for e in resp.get("Errors", []) if e["ErrorDetail"]["ErrorCode"] != "AlreadyExistsException"]
    if errors:
        raise RuntimeError(f"batch_create_partition: {errors}")
    return not resp.get("Errors")


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    key = unquote_plus(event["detail"]["object"]["key"])
    parsed = parse_key(key)
    if not parsed:
        return {"registered": 0, "ignored": 1}
    cfg, value, dt, hh = parsed
    created = register(cfg, value, dt, hh)
    logger.info(json.dumps({"message": "partition", "table": cfg["table"], "column": cfg["column"],
                            "values": [value, dt, hh], "created": created}))
    return {"registered": int(created), "ignored": 0}
