"""Particionamento opcional do RAW por uma coluna da tabela (`partition_by` em tables/<t>.yaml).

Módulo puro (sem boto3), usado pela Lambda e pelo script de snapshot, para que ambos
gerem EXATAMENTE o mesmo caminho de partição.

Layout:  <prefix>/<coluna>=<valor>/dt=YYYY-MM-DD/hh=HH/<arquivo>.json.gz
Valor normalizado: caracteres fora de [A-Za-z0-9._-] viram "_"; ausente/vazio/tipo não escalar
vira "__NULL__". O valor ORIGINAL continua intacto em NewImage/OldImage.
"""
from __future__ import annotations

import re
from typing import Any

NULL_VALUE = "__NULL__"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def normalize(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _UNSAFE.sub("_", text)
    return text or NULL_VALUE


def attr_value(attr: dict | None) -> str | None:
    """Valor escalar de um atributo DynamoDB JSON ({"S": "x"}, {"N": "1"}, {"BOOL": true})."""
    if not attr:
        return None
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        return attr["N"]
    if "BOOL" in attr:
        return "true" if attr["BOOL"] else "false"
    return None  # NULL, M, L, SS, ... não são particionáveis


def partition_value(new_image: dict | None, old_image: dict | None, column: str) -> str:
    """Valor da partição de um evento. REMOVE não tem NewImage: usa OldImage."""
    image = new_image or old_image or {}
    return normalize(attr_value(image.get(column)))


def partition_dir(column: str, value: str) -> str:
    # Hive/Glue usam nomes de partição em minúsculas
    return f"{column.lower()}={value}/"
