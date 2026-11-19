"""Geradores determinísticos de dados fictícios (sem dependências externas).

Mesmo seed => mesmos dados. Os ids são previsíveis (C00001, P0001, ...), então
vendas/estoque referenciam clientes/produtos sem consultar o DynamoDB.
Os atributos seguem o schema declarado em tables/*.yaml.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterator

NOMES = ["Ana", "Bruno", "Carla", "Daniel", "Elisa", "Felipe", "Gabriela", "Hugo", "Isabela", "João",
         "Karen", "Lucas", "Marina", "Nicolas", "Olivia", "Paulo", "Queila", "Rafael", "Sofia", "Tiago"]
SOBRENOMES = ["Silva", "Souza", "Oliveira", "Santos", "Lima", "Pereira", "Costa", "Ribeiro", "Alves",
              "Carvalho", "Gomes", "Martins", "Rocha", "Barbosa", "Araújo"]
CIDADES = [("São Paulo", "SP"), ("Rio de Janeiro", "RJ"), ("Belo Horizonte", "MG"), ("Curitiba", "PR"),
           ("Porto Alegre", "RS"), ("Salvador", "BA"), ("Recife", "PE"), ("Fortaleza", "CE"),
           ("Brasília", "DF"), ("Goiânia", "GO")]
CATEGORIAS = {
    "Eletrônicos": ["Fone", "Carregador", "Mouse", "Teclado", "Webcam", "Caixa de som"],
    "Casa": ["Panela", "Jogo de toalhas", "Luminária", "Cafeteira", "Organizador"],
    "Esporte": ["Bola", "Tênis", "Garrafa térmica", "Corda de pular", "Mochila"],
    "Livros": ["Romance", "Biografia", "Guia técnico", "HQ", "Livro infantil"],
    "Beleza": ["Shampoo", "Hidratante", "Perfume", "Protetor solar", "Sabonete"],
}
STATUS = ["PAGO", "PAGO", "PAGO", "ENVIADO", "ENTREGUE", "ENTREGUE", "CANCELADO", "PENDENTE"]

BASE_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def cliente_id(n: int) -> str:
    return f"C{n:05d}"


def produto_id(n: int) -> str:
    return f"P{n:04d}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def gen_clientes(count: int, seed: int = 42) -> Iterator[dict]:
    rnd = random.Random(f"clientes-{seed}")
    for n in range(1, count + 1):
        nome = f"{rnd.choice(NOMES)} {rnd.choice(SOBRENOMES)}"
        cidade, uf = rnd.choice(CIDADES)
        yield {
            "id": cliente_id(n),
            "nome": nome,
            "email": f"{nome.lower().replace(' ', '.')}.{n}@exemplo.com.br".replace("ã", "a").replace("ú", "u"),
            "ativo": rnd.random() < 0.9,
            "criado_em": _iso(BASE_DATE - timedelta(days=rnd.randint(1, 900), seconds=rnd.randint(0, 86399))),
            "cidade": cidade,
            "uf": uf,
        }


def gen_produtos(count: int, seed: int = 42) -> Iterator[dict]:
    rnd = random.Random(f"produtos-{seed}")
    cats = list(CATEGORIAS)
    for n in range(1, count + 1):
        cat = rnd.choice(cats)
        yield {
            "id": produto_id(n),
            "nome": f"{rnd.choice(CATEGORIAS[cat])} {rnd.choice(['Pro', 'Plus', 'Max', 'Lite', 'Eco'])} {n}",
            "categoria": cat,
            "preco": Decimal(rnd.randint(990, 49990)) / 100,
            "ativo": rnd.random() < 0.95,
        }


def gen_estoque(produtos: int, seed: int = 42) -> Iterator[dict]:
    rnd = random.Random(f"estoque-{seed}")
    for n in range(1, produtos + 1):
        yield {
            "produto_id": produto_id(n),
            "quantidade": rnd.randint(0, 500),
            "atualizado_em": _iso(BASE_DATE + timedelta(days=rnd.randint(0, 600))),
        }


def gen_faturamento(count: int, clientes: int = 500, seed: int = 42) -> Iterator[dict]:
    rnd = random.Random(f"faturamento-{seed}")
    for n in range(1, count + 1):
        yield {
            "fatura_id": f"F{n:06d}",
            "cliente_id": cliente_id(rnd.randint(1, clientes)),
            "valor": (Decimal(rnd.randint(1000, 500000)) / 100).quantize(Decimal("0.01")),
            "data_emissao": _iso(BASE_DATE + timedelta(days=rnd.randint(0, 600), seconds=rnd.randint(0, 86399))),
        }


def gen_vendas(count: int, clientes: int, produtos: int, seed: int = 42) -> Iterator[dict]:
    rnd = random.Random(f"vendas-{seed}")
    # precos dos produtos, para valor_total coerente
    precos = {p["id"]: p["preco"] for p in gen_produtos(produtos, seed)}
    for n in range(1, count + 1):
        pid = produto_id(rnd.randint(1, produtos))
        qtd = rnd.randint(1, 5)
        status = rnd.choice(STATUS)
        yield {
            "cliente_id": cliente_id(rnd.randint(1, clientes)),
            "venda_id": f"V{n:07d}",
            "produto_id": pid,
            "quantidade": qtd,
            "valor_total": (precos[pid] * qtd).quantize(Decimal("0.01")),
            "data": _iso(BASE_DATE + timedelta(days=rnd.randint(0, 600), seconds=rnd.randint(0, 86399))),
            "status": status,
            "pago": status in ("PAGO", "ENVIADO", "ENTREGUE"),
        }
