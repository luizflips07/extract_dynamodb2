"""Atende o pedido de clone (modelo de issue clonar-repositorio.yml).

Lê ISSUE_BODY, valida os campos obrigatórios e grava na CÓPIA:
  config.yaml     project, tfstate_prefix (= project), region, datalake.account_id, datalake.raw_bucket
                  e a região/conta do raw_kms_key_arn (o id da chave continua de exemplo)
  sources/*.yaml  account_id
Escreve `repo_name`/`project` em GITHUB_OUTPUT. Só biblioteca padrão: roda no runner sem instalar nada.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ci_config import set_value

# devem ser iguais aos `label` do modelo de issue
NAME_FIELD = "Nome do novo repositório"
PROJECT_FIELD = "Nome do projeto"
REGION_FIELD = "Região AWS"
BUCKET_FIELD = "Nome do bucket RAW"
ACCOUNT_FIELD = "Número da conta AWS"
FIELDS = (NAME_FIELD, PROJECT_FIELD, REGION_FIELD, BUCKET_FIELD, ACCOUNT_FIELD)

RULES = {
    NAME_FIELD: (r"(?!\.+$)[A-Za-z0-9._-]{1,100}", "letras, números, ponto, hífen e sublinhado"),
    # vira nome de role/Lambda/fila (limite de 64 caracteres) e pasta no bucket de state
    PROJECT_FIELD: (r"[a-z][a-z0-9-]{1,28}[a-z0-9]", "3 a 30: minúsculas, números e '-', começando por letra"),
    REGION_FIELD: (r"[a-z]{2}(-gov)?-[a-z]+-\d", "ex.: us-east-1"),
    # o bucket de logs é <bucket>-access-logs e o limite do S3 é 63
    BUCKET_FIELD: (r"[a-z0-9][a-z0-9.-]{1,49}[a-z0-9]", "3 a 51: minúsculas, números, ponto e hífen"),
    ACCOUNT_FIELD: (r"\d{12}", "12 dígitos"),
}

PROJECT_LINE = re.compile(r"^project:[ \t]*([^\s#]+)", re.MULTILINE)


def field(body: str, label: str) -> str:
    """O formulário gera '### <label>' seguido da resposta."""
    lines = [line.strip() for line in body.splitlines()]
    try:
        start = lines.index(f"### {label}") + 1
    except ValueError:
        raise ValueError(f"campo '{label}' não encontrado: abra a issue pelo modelo") from None
    answer = next((line for line in lines[start:] if line), "")
    return "" if answer.startswith("###") or answer == "_No response_" else answer


def parse(body: str) -> dict[str, str]:
    """Todos os campos são obrigatórios; informa de uma vez tudo o que está errado."""
    values, errors = {}, []
    for label in FIELDS:
        values[label] = field(body, label)
        pattern, hint = RULES[label]
        if not values[label]:
            errors.append(f"'{label}' é obrigatório")
        elif not re.fullmatch(pattern, values[label]):
            errors.append(f"'{label}' inválido ({hint})")
    if errors:
        raise ValueError("; ".join(errors))
    return values


def apply_config(config: str, values: dict[str, str]) -> str:
    """Troca só as linhas necessárias, preservando comentários."""
    project, region = values[PROJECT_FIELD], values[REGION_FIELD]
    account, bucket = values[ACCOUNT_FIELD], values[BUCKET_FIELD]
    current = PROJECT_LINE.search(config)
    if not current:
        raise ValueError("config.yaml sem a linha `project:`")
    if current.group(1).strip("\"'") == project:
        raise ValueError(f"project '{project}' é o mesmo deste repositório: os recursos e o state colidiriam")
    datalake = config.index("\ndatalake:")
    head, tail = config[:datalake], config[datalake:]
    head = set_value(head, "project", project)
    head = set_value(head, "tfstate_prefix", f'"{project}"')
    head = set_value(head, "region", region)
    tail = set_value(tail, "account_id", f'"{account}"')
    tail = set_value(tail, "raw_bucket", f'"{bucket}"')
    key = "00000000-0000-0000-0000-000000000000"  # a CMK ainda não existe na conta nova
    tail = set_value(tail, "raw_kms_key_arn", f'"arn:aws:kms:{region}:{account}:key/{key}"')
    return head + tail


def main() -> int:
    root = Path(os.environ.get("ROOT_PATH", "."))
    try:
        values = parse(os.environ.get("ISSUE_BODY", ""))
        config = root / "config.yaml"
        updates = {config: apply_config(config.read_bytes().decode("utf-8"), values)}
        for source in sorted((root / "sources").glob("*.yaml")):
            text = source.read_bytes().decode("utf-8")
            updates[source] = set_value(text, "account_id", f'"{values[ACCOUNT_FIELD]}"')
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    for path, text in updates.items():
        path.write_bytes(text.encode("utf-8"))
    name, project = values[NAME_FIELD], values[PROJECT_FIELD]
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"repo_name={name}\nproject={project}\n")
    print(f"repositório={name} project={project}")  # conta e bucket ficam fora do log
    return 0


if __name__ == "__main__":
    sys.exit(main())
