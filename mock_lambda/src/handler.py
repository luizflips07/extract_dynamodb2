"""Lambda do MOCK: cria as tabelas DynamoDB de exemplo e carrega dados fictícios.

Faz o mesmo que mock/terraform + mock/data/load_data.py, mas dentro de uma Lambda.
É implantada e invocada MANUALMENTE (mock_lambda/terraform e mock_lambda/invoke.py).
Reaproveita mock/data/generate.py (empacotado junto pelo Terraform).

Evento (todos os campos são opcionais):
{
  "action": "all",                       # create_tables | load_data | all | delete_tables
  "tables": ["clientes", "faturamento"], # padrão: todas
  "prefix": "",                          # prefixo dos nomes das tabelas
  "counts": {"clientes": 500, "produtos": 100, "vendas": 100000, "faturamento": 200},
  "seed": 42,
  "confirm": false                       # obrigatório (true) em delete_tables
}
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice

import boto3
from botocore.exceptions import ClientError

import generate as gen

# Chaves alinhadas com mock/terraform/main.tf e tables/*.yaml
TABLES = {
    "clientes": ("id", None),
    "produtos": ("id", None),
    "vendas": ("cliente_id", "venda_id"),
    "estoque": ("produto_id", None),
    "faturamento": ("fatura_id", None),
}
ORDER = ["clientes", "produtos", "estoque", "vendas", "faturamento"]  # ordem de dependência
DEFAULT_COUNTS = {"clientes": 500, "produtos": 100, "vendas": 100_000, "faturamento": 200}
CHUNK = 500
WORKERS = 8
SAFETY_MARGIN_MS = 20_000  # para de gravar antes do timeout da Lambda

ddb = boto3.client("dynamodb")
ddb_res = boto3.resource("dynamodb")


def counts_from(event: dict) -> dict:
    c = dict(DEFAULT_COUNTS)
    c.update({k: int(v) for k, v in (event.get("counts") or {}).items()})
    c["estoque"] = c["produtos"]  # 1 por produto
    return c


def make_items(name: str, c: dict, seed: int):
    return {
        "clientes": lambda: gen.gen_clientes(c["clientes"], seed),
        "produtos": lambda: gen.gen_produtos(c["produtos"], seed),
        "estoque": lambda: gen.gen_estoque(c["produtos"], seed),
        "vendas": lambda: gen.gen_vendas(c["vendas"], c["clientes"], c["produtos"], seed),
        "faturamento": lambda: gen.gen_faturamento(c["faturamento"], c["clientes"], seed),
    }[name]()


def create_table(full_name: str, hash_key: str, range_key: str | None) -> str:
    try:
        ddb.describe_table(TableName=full_name)
        return "exists"
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    attrs = [{"AttributeName": hash_key, "AttributeType": "S"}]
    schema = [{"AttributeName": hash_key, "KeyType": "HASH"}]
    if range_key:
        attrs.append({"AttributeName": range_key, "AttributeType": "S"})
        schema.append({"AttributeName": range_key, "KeyType": "RANGE"})
    ddb.create_table(
        TableName=full_name,
        AttributeDefinitions=attrs,
        KeySchema=schema,
        BillingMode="PAY_PER_REQUEST",
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
        SSESpecification={"Enabled": True},
        Tags=[{"Key": "purpose", "Value": "mock"}, {"Key": "managed-by", "Value": "mock-lambda"}],
    )
    ddb.get_waiter("table_exists").wait(TableName=full_name, WaiterConfig={"Delay": 3, "MaxAttempts": 40})
    # PITR (necessário para a carga inicial/export); pode levar alguns segundos para ficar disponível
    for attempt in range(10):
        try:
            ddb.update_continuous_backups(
                TableName=full_name, PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True}
            )
            break
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ContinuousBackupsUnavailableException", "ResourceInUseException"):
                raise
            time.sleep(3 + attempt)
    return "created"


def chunks(it, size):
    it = iter(it)
    while True:
        block = list(islice(it, size))
        if not block:
            return
        yield block


def write_block(table, block) -> int:
    with table.batch_writer() as bw:
        for item in block:
            bw.put_item(Item=item)
    return len(block)


def load_table(full_name: str, items, deadline_ms_fn) -> dict:
    table = ddb_res.Table(full_name)
    total, truncated = 0, False
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        # processa em "ondas" de WORKERS blocos para poder parar antes do timeout
        gen_chunks = chunks(items, CHUNK)
        while True:
            wave = list(islice(gen_chunks, WORKERS))
            if not wave:
                break
            if deadline_ms_fn() < SAFETY_MARGIN_MS:
                truncated = True
                break
            total += sum(pool.map(lambda b: write_block(table, b), wave))
    return {"items": total, "truncated": truncated}


def handler(event, context):
    event = event or {}
    action = event.get("action", "all")
    prefix = event.get("prefix", "")
    seed = int(event.get("seed", 42))
    names = event.get("tables") or ORDER
    unknown = [n for n in names if n not in TABLES]
    if unknown:
        raise ValueError(f"tabelas desconhecidas: {unknown}; válidas: {list(TABLES)}")
    if action not in ("create_tables", "load_data", "all", "delete_tables"):
        raise ValueError("action inválida")
    names = [n for n in ORDER if n in names]
    c = counts_from(event)
    remaining = getattr(context, "get_remaining_time_in_millis", lambda: 10**9)
    out = {"action": action, "prefix": prefix, "tables": {}}

    if action == "delete_tables":
        if event.get("confirm") is not True:
            raise ValueError('delete_tables exige "confirm": true')
        for n in names:
            try:
                ddb.delete_table(TableName=f"{prefix}{n}")
                out["tables"][n] = {"delete": "requested"}
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                out["tables"][n] = {"delete": "not_found"}
        return out

    for n in names:
        full = f"{prefix}{n}"
        res = {}
        if action in ("create_tables", "all"):
            res["create"] = create_table(full, *TABLES[n])
        if action in ("load_data", "all"):
            started = time.time()
            res["load"] = load_table(full, make_items(n, c, seed), remaining)
            res["load"]["seconds"] = round(time.time() - started, 1)
        out["tables"][n] = res
        print(out["tables"][n] | {"table": full})
    return out
