"""Invoca LOCALMENTE (manual) a Lambda do mock.

Exemplos:
  python mock_lambda/invoke.py                                     # cria as 5 tabelas e carrega tudo
  python mock_lambda/invoke.py --action create_tables              # só cria as tabelas
  python mock_lambda/invoke.py --action load_data --tables faturamento --faturamento 500
  python mock_lambda/invoke.py --tables clientes vendas --vendas 20000
  python mock_lambda/invoke.py --action delete_tables --tables faturamento --confirm
  python mock_lambda/invoke.py --dry-run                           # só mostra o payload

Usa as credenciais/região do `aws configure` (ou --region / --profile).
"""
from __future__ import annotations

import argparse
import json
import sys

TABLES = ["clientes", "produtos", "estoque", "vendas", "faturamento"]


def build_payload(a) -> dict:
    counts = {k: getattr(a, k) for k in ("clientes", "produtos", "vendas", "faturamento") if getattr(a, k) is not None}
    payload = {"action": a.action, "prefix": a.prefix, "seed": a.seed}
    if a.tables:
        payload["tables"] = a.tables
    if counts:
        payload["counts"] = counts
    if a.confirm:
        payload["confirm"] = True
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--function-name", default="mock-dynamodb-loader")
    ap.add_argument("--action", default="all", choices=["all", "create_tables", "load_data", "delete_tables"])
    ap.add_argument("--tables", nargs="+", choices=TABLES)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--clientes", type=int)
    ap.add_argument("--produtos", type=int)
    ap.add_argument("--vendas", type=int)
    ap.add_argument("--faturamento", type=int)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--confirm", action="store_true", help="obrigatório em delete_tables")
    ap.add_argument("--region")
    ap.add_argument("--profile")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    payload = build_payload(a)
    print("payload:", json.dumps(payload))
    if a.dry_run:
        return 0

    import boto3
    from botocore.config import Config

    session = boto3.Session(region_name=a.region, profile_name=a.profile)
    if not session.region_name:
        ap.error("região não definida: rode `aws configure` ou use --region")
    print("conta", session.client("sts").get_caller_identity()["Account"], "| região", session.region_name)
    # a Lambda pode rodar até 15 min: o cliente espera mais que isso
    lam = session.client("lambda", config=Config(read_timeout=910, retries={"max_attempts": 0}))
    resp = lam.invoke(FunctionName=a.function_name, Payload=json.dumps(payload).encode())
    body = json.loads(resp["Payload"].read() or "null")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    if resp.get("FunctionError"):
        print("A Lambda retornou erro:", resp["FunctionError"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
