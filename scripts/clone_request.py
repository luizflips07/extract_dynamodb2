"""Atende o pedido de clone (modelo de issue clonar-repositorio.yml).

Lê ISSUE_BODY, valida o nome do repositório e o `project`, grava no config.yaml da cópia o novo
`project` e `tfstate_prefix` (= project) e escreve `repo_name`/`project` em GITHUB_OUTPUT.
Só biblioteca padrão: roda no runner sem instalar nada.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# devem ser iguais aos `label` do modelo de issue
NAME_FIELD = "Nome do novo repositório"
PROJECT_FIELD = "Project (prefixo dos recursos e do tfstate)"

VALID_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
# vira nome de role/Lambda/fila (limite de 64 caracteres com fonte e sufixo) e pasta no bucket de state
VALID_PROJECT = re.compile(r"[a-z][a-z0-9-]{1,28}[a-z0-9]")

PROJECT_LINE = re.compile(r"^project:[ \t]*([^\s#]+)", re.MULTILINE)
PREFIX_LINE = re.compile(r'^tfstate_prefix:[ \t]*(""|[^\s#]*)', re.MULTILINE)


def field(body: str, label: str) -> str:
    """O formulário gera '### <label>' seguido da resposta."""
    lines = [line.strip() for line in body.splitlines()]
    try:
        start = lines.index(f"### {label}") + 1
    except ValueError:
        raise ValueError(f"campo '{label}' não encontrado: abra a issue pelo modelo") from None
    return next((line for line in lines[start:] if line), "")


def repo_name(body: str) -> str:
    name = field(body, NAME_FIELD)
    if not VALID_NAME.fullmatch(name) or set(name) == {"."}:
        raise ValueError(f"nome inválido: {name!r}")
    return name


def project_name(body: str) -> str:
    project = field(body, PROJECT_FIELD)
    if not VALID_PROJECT.fullmatch(project):
        raise ValueError(f"project inválido: {project!r} (3 a 30: minúsculas, números e '-', começando por letra)")
    return project


def apply_project(config: str, project: str) -> str:
    """Troca só as linhas `project` e `tfstate_prefix`, preservando comentários."""
    current = PROJECT_LINE.search(config)
    if not current or not PREFIX_LINE.search(config):
        raise ValueError("config.yaml sem as linhas `project:` e `tfstate_prefix:`")
    if current.group(1).strip("\"'") == project:
        raise ValueError(f"project '{project}' é o mesmo deste repositório: os recursos e o state colidiriam")
    config = PROJECT_LINE.sub(f"project: {project}", config, count=1)
    return PREFIX_LINE.sub(f'tfstate_prefix: "{project}"', config, count=1)


def main() -> int:
    body = os.environ.get("ISSUE_BODY", "")
    path = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
    try:
        name, project = repo_name(body), project_name(body)
        updated = apply_project(path.read_bytes().decode("utf-8"), project)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    path.write_bytes(updated.encode("utf-8"))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"repo_name={name}\nproject={project}\n")
    print(f"repositório={name} project={project}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
