"""Carrega dados fictícios nas tabelas mock (execução MANUAL).

Padrão: 500 clientes, 100 produtos, 100 estoque (1 por produto), 100.000 vendas, 200 faturas.

Exemplos:
  python mock/data/load_data.py                       # tudo
  python mock/data/load_data.py --tables clientes produtos
  python mock/data/load_data.py --vendas 1000         # carga reduzida
  python mock/data/load_data.py --dry-run             # só gera e conta, não escreve

ATENÇÃO: cada item gravado vira evento no DynamoDB Streams. Se o pipeline (ESM) já estiver
ativo, os eventos fluem para o RAW. Para validar a carga inicial, carregue ANTES de aplicar
infra/producer e rode scripts/initial_load.py.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import generate as gen  # noqa: E402

CHUNK = 500  # itens por tarefa; cada tarefa usa batch_writer (25 itens/requisição)


def chunks(it, size):
    it = iter(it)
    while True:
        block = list(islice(it, size))
        if not block:
            return
        yield block


def write_block(table, block):
    with table.batch_writer() as bw:  # trata UnprocessedItems com retry
        for item in block:
            bw.put_item(Item=item)
    return len(block)


def load(ddb, name, items, workers, dry_run):
    total = 0
    started = time.time()
    if dry_run:
        total = sum(1 for _ in items)
    else:
        table = ddb.Table(name)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for n in pool.map(lambda b: write_block(table, b), chunks(items, CHUNK)):
                total += n
                print(f"\r{name}: {total}", end="", flush=True)
    print(f"\r{name}: {total} itens em {time.time() - started:.1f}s" + (" (dry-run)" if dry_run else ""))
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", nargs="+", default=["clientes", "produtos", "estoque", "vendas", "faturamento"],
                    choices=["clientes", "produtos", "estoque", "vendas", "faturamento"])
    ap.add_argument("--clientes", type=int, default=500)
    ap.add_argument("--produtos", type=int, default=100)
    ap.add_argument("--faturamento", type=int, default=200)
    ap.add_argument("--vendas", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--region", default=None, help="padrão: região do `aws configure` (AWS_REGION/AWS_DEFAULT_REGION)")
    ap.add_argument("--prefix", default="", help="prefixo dos nomes de tabela (igual ao do terraform)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ddb = None
    if not a.dry_run:
        import boto3

        session = boto3.Session(region_name=a.region)  # None => credenciais/região do aws configure
        if not session.region_name:
            ap.error("região não definida: rode `aws configure` ou use --region")
        who = session.client("sts").get_caller_identity()["Account"]
        print(f"Conta {who} | região {session.region_name}")
        ddb = session.resource("dynamodb")

    plan = {
        "clientes": lambda: gen.gen_clientes(a.clientes, a.seed),
        "produtos": lambda: gen.gen_produtos(a.produtos, a.seed),
        "estoque": lambda: gen.gen_estoque(a.produtos, a.seed),
        "vendas": lambda: gen.gen_vendas(a.vendas, a.clientes, a.produtos, a.seed),
        "faturamento": lambda: gen.gen_faturamento(a.faturamento, a.clientes, a.seed),
    }
    for name in ["clientes", "produtos", "estoque", "vendas", "faturamento"]:  # ordem de dependência
        if name in a.tables:
            load(ddb, f"{a.prefix}{name}", plan[name](), a.workers, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
