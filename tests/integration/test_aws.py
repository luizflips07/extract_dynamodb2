"""Testes de integração contra a AWS (a stack producer já precisa estar aplicada).

Ficam DESLIGADOS no `pytest` normal e no CI. Como executar: docs/TESTES.md.

  RUN_AWS_TESTS=1  liga parâmetros, recursos e falhas forçadas
  RUN_AWS_FLOW=1   liga também o fluxo DynamoDB -> RAW (grava e apaga um item; demora minutos)
  TEST_SOURCE      fonte (sources/<nome>.yaml); padrão: local
  TEST_TABLE       tabela usada na falha e no fluxo; padrão: a primeira da fonte
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AWS_TESTS") != "1", reason="teste na AWS: defina RUN_AWS_TESTS=1"
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = os.environ.get("TEST_SOURCE", "local")


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def env():
    import boto3

    cfg = _yaml(ROOT / "config.yaml")
    source = _yaml(ROOT / "sources" / f"{SOURCE}.yaml")
    session = boto3.Session(region_name=cfg["region"])
    account = session.client("sts").get_caller_identity()["Account"]
    # Guarda: não rodar na conta errada (nem com os valores de exemplo do repositório)
    if account != source["account_id"]:
        pytest.fail(f"Credenciais da conta {account}, mas sources/{SOURCE}.yaml aponta {source['account_id']}.")

    files = {p: _yaml(p) for p in sorted((ROOT / "tables").glob("*.yaml"))}
    files = {p: t for p, t in files.items() if t["source"] == SOURCE}
    tables = {t["table_name"]: t for t in files.values()}
    prefix = f"{cfg['project']}-{SOURCE}"
    sqs = session.client("sqs")
    return {
        "cfg": cfg,
        "session": session,
        "account": account,
        "tables": tables,
        "files": {t["table_name"]: p for p, t in files.items()},
        "table": os.environ.get("TEST_TABLE") or next(iter(tables)),
        "function": f"{prefix}-writer",
        "params_table": f"{prefix}-parametros",
        "queue_url": sqs.get_queue_url(QueueName=f"{prefix}-on-failure")["QueueUrl"],
        "sqs": sqs,
    }


# ---------- Tabela de parâmetros ----------
def test_parameter_table_mirrors_yaml_files(env):
    ddb = env["session"].client("dynamodb")
    items = {}
    for page in ddb.get_paginator("scan").paginate(TableName=env["params_table"], ConsistentRead=True):
        items.update({i["nome_tabela"]["S"]: i for i in page["Items"]})

    assert set(items) == set(env["tables"])
    for name, item in items.items():
        yaml_text = env["files"][name].read_text(encoding="utf-8").replace("\r\n", "\n")
        assert item["contrato"]["S"] == yaml_text, f"contrato de {name} difere do YAML: falta aplicar o Terraform?"
        created = datetime.fromisoformat(item["data_criacao"]["S"].replace("Z", "+00:00"))
        updated = datetime.fromisoformat(item["ultimo_update"]["S"].replace("Z", "+00:00"))
        assert created <= updated


# ---------- Recursos ----------
def test_every_stream_mapping_is_enabled_and_uses_the_single_queue(env):
    attrs = env["sqs"].get_queue_attributes(QueueUrl=env["queue_url"], AttributeNames=["QueueArn"])
    queue_arn = attrs["Attributes"]["QueueArn"]
    lam = env["session"].client("lambda")
    mappings = []
    for page in lam.get_paginator("list_event_source_mappings").paginate(FunctionName=env["function"]):
        mappings += page["EventSourceMappings"]

    mapped = {m["EventSourceArn"].split(":table/")[1].split("/stream/")[0] for m in mappings}
    assert mapped == set(env["tables"])
    for m in mappings:
        assert m["State"] == "Enabled"
        assert m["DestinationConfig"]["OnFailure"]["Destination"] == queue_arn


# ---------- Formato das tabelas Glue (após deploy/recriação) ----------
def test_glue_tables_match_the_yaml_format(env):
    sys.path.insert(0, str(ROOT / "scripts"))
    import table_sync

    glue = env["session"].client("glue")
    database = env["cfg"]["datalake"].get("glue_database", "raw")
    for table in env["tables"].values():
        current = glue.get_table(DatabaseName=database, Name=table_sync.glue_table_name(table))["Table"]
        assert table_sync.classify(table, current) == "unchanged", f"{table['table_name']}: falta o deploy do formato novo"


# ---------- Falhas forçadas ----------
def _unique_sequence() -> str:
    # Única por execução (a Lambda não repete aviso do mesmo lote) e longa o bastante para a chave
    # passar de 1024 bytes: o S3 recusa e NADA é gravado na RAW.
    return str(time.time_ns()).ljust(600, "9")


def _invoke_with_fake_batch(env, stream_arn: str, seq: str) -> dict:
    record = {
        "eventSourceARN": stream_arn,
        "eventID": "teste-integracao",
        "eventName": "INSERT",
        "dynamodb": {"Keys": {}, "NewImage": {}, "SequenceNumber": seq,
                     "ApproximateCreationDateTime": int(time.time()), "SizeBytes": 1},
    }
    out = env["session"].client("lambda").invoke(
        FunctionName=env["function"], Payload=json.dumps({"Records": [record]})
    )
    assert "FunctionError" not in out
    return json.loads(out["Payload"].read())


def _take_message(env, seq: str, timeout: int = 60):
    """Acha a mensagem deste teste, apaga SÓ ela e devolve as outras à fila."""
    sqs, url = env["sqs"], env["queue_url"]
    others, found = [], None
    deadline = time.time() + timeout
    while found is None and time.time() < deadline:
        batch = sqs.receive_message(
            QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=5,
            VisibilityTimeout=timeout, MessageAttributeNames=["All"],
        ).get("Messages", [])
        for message in batch:
            body = json.loads(message["Body"])
            if body.get("primeiro_sequence") == seq:
                found = (message, body)
                sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
            else:
                others.append(message["ReceiptHandle"])
    for handle in others:
        sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=handle, VisibilityTimeout=0)
    assert found, "mensagem do teste não apareceu na fila"
    return found


def test_failed_batch_sends_table_parameters_to_the_queue(env):
    table, seq = env["table"], _unique_sequence()
    stream = env["session"].client("dynamodb").describe_table(TableName=table)["Table"]["LatestStreamArn"]

    assert _invoke_with_fake_batch(env, stream, seq) == {"batchItemFailures": [{"itemIdentifier": seq}]}

    message, body = _take_message(env, seq)
    assert message["MessageAttributes"]["tabela"]["StringValue"] == table
    assert body["tipo"] == "erro_processamento"
    assert body["tabela"] == table
    assert "KeyTooLongError" in body["erro"]
    assert body["parametros"]["keys"] == env["tables"][table]["keys"]
    assert body["parametros"]["columns"] == env["tables"][table]["columns"]
    assert body["parametros"]["streaming"]["batch_size"] > 0


def test_unconfigured_table_is_reported_without_parameters(env):
    seq = _unique_sequence()
    arn = (f"arn:aws:dynamodb:{env['cfg']['region']}:{env['account']}:"
           "table/tabela-nao-configurada/stream/2026-01-01T00:00:00.000")

    assert _invoke_with_fake_batch(env, arn, seq) == {"batchItemFailures": [{"itemIdentifier": seq}]}

    _, body = _take_message(env, seq)
    assert body["tabela"] == "tabela-nao-configurada"
    assert body["erro"].startswith("table_not_configured")
    assert body["parametros"] is None


# ---------- Fluxo completo até a RAW ----------
@pytest.mark.skipif(os.environ.get("RUN_AWS_FLOW") != "1", reason="fluxo completo: defina RUN_AWS_FLOW=1")
def test_new_item_reaches_raw(env):
    table = env["tables"][env["table"]]
    name = table["table_name"]
    marker = f"TESTE-{time.time_ns()}"
    types = {c["name"]: c["dynamo"] for c in table["columns"]}
    key = {k: {types[k]: str(time.time_ns()) if types[k] == "N" else marker} for k in table["keys"]}
    needle = next(iter(key[table["keys"][0]].values()))

    window = table.get("streaming", {}).get(
        "max_batching_window_seconds", env["cfg"]["streaming"]["max_batching_window_seconds"]
    )
    bucket = env["cfg"]["datalake"]["raw_bucket"]
    # Leitura da RAW pode exigir credencial da conta do Data Lake
    import boto3

    profile = os.environ.get("AWS_DATALAKE_PROFILE")
    reader = boto3.Session(profile_name=profile, region_name=env["cfg"]["region"]) if profile else env["session"]
    s3 = reader.client("s3")
    ddb = env["session"].client("dynamodb")

    start = datetime.now(timezone.utc).replace(microsecond=0)
    ddb.put_item(TableName=name, Item=key)
    try:
        deadline = time.time() + window + 180
        seen: set[str] = set()
        while time.time() < deadline:
            time.sleep(15)
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=f"dynamodb/{SOURCE}/{name}/"
            ):
                for obj in page.get("Contents", []):
                    if obj["Key"] in seen or obj["LastModified"] < start:
                        continue
                    seen.add(obj["Key"])
                    raw = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                    for line in gzip.decompress(raw).decode("utf-8").splitlines():
                        record = json.loads(line)
                        if needle in json.dumps(record["Keys"]):
                            assert record["_table"] == name
                            assert record["eventName"] == "INSERT"
                            return
        pytest.fail(f"item {needle} não chegou à RAW em {window + 180} s")
    finally:
        ddb.delete_item(TableName=name, Key=key)  # a view Trusted descarta o item após o REMOVE
