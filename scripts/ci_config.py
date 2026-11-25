"""No GitHub Actions, troca os valores de exemplo de config.yaml e sources/*.yaml pelos reais,
vindos de secrets. Assim o repositório (público) nunca guarda conta, bucket, chave ou e-mail.

  DATALAKE_ACCOUNT_ID  conta do Data Lake
  RAW_BUCKET           bucket RAW
  RAW_KMS_KEY_ID       id da CMK do bucket RAW (saída do job datalake); vira o ARN em config.yaml
  KMS_OPTIONAL         "1" no job datalake, que cria a CMK e ainda não tem o id
  ALARM_EMAILS         e-mails separados por vírgula
  SOURCE_ACCOUNT_IDS   fonte=conta, separadas por vírgula (ex.: local=123456789012)

Variável vazia mantém o que está no arquivo. Se sobrar valor de exemplo, o script falha.
Altera os arquivos no lugar: use só no runner. Só biblioteca padrão.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PLACEHOLDERS = ("111111111111", "222222222222", "meu-datalake-raw-001", "seu-email@exemplo.com")
SAFE = re.compile(r"[A-Za-z0-9._:/@+-]+")  # impede que um valor quebre o YAML


def _check(name: str, value: str) -> str:
    if not SAFE.fullmatch(value):
        raise ValueError(f"valor inválido em {name}")
    return value


def set_value(text: str, key: str, value: str) -> str:
    """Troca o valor de `key:` preservando indentação e comentário."""
    pattern = re.compile(rf'^([ \t]*{key}:[ \t]*)("[^"]*"|\[[^\]]*\]|[^\s#]+)', re.MULTILINE)
    out, n = pattern.subn(lambda m: m.group(1) + value, text, count=1)
    if n != 1:
        raise ValueError(f"linha `{key}:` não encontrada")
    return out


def render_config(text: str, env: dict[str, str]) -> str:
    datalake = text.index("\ndatalake:")  # `account_id` e demais chaves são os do bloco datalake
    head, tail = text[:datalake], text[datalake:]
    for var, key in (("DATALAKE_ACCOUNT_ID", "account_id"), ("RAW_BUCKET", "raw_bucket")):
        if env.get(var):
            tail = set_value(tail, key, f'"{_check(var, env[var].strip())}"')
    if env.get("RAW_KMS_KEY_ID"):
        region = re.search(r"^region:[ \t]*([a-z0-9-]+)", head, re.MULTILINE).group(1)
        account = _check("DATALAKE_ACCOUNT_ID", env.get("DATALAKE_ACCOUNT_ID", "").strip())
        key_id = _check("RAW_KMS_KEY_ID", env["RAW_KMS_KEY_ID"].strip())
        tail = set_value(tail, "raw_kms_key_arn", f'"arn:aws:kms:{region}:{account}:key/{key_id}"')
    emails = [e.strip() for e in env.get("ALARM_EMAILS", "").split(",") if e.strip()]
    if emails:
        quoted = ", ".join(f'"{_check("ALARM_EMAILS", e)}"' for e in emails)
        tail = set_value(tail, "notification_emails", f"[{quoted}]")
    return head + tail


def source_accounts(env: dict[str, str]) -> dict[str, str]:
    pairs = [p.strip() for p in env.get("SOURCE_ACCOUNT_IDS", "").split(",") if p.strip()]
    out = {}
    for pair in pairs:
        name, _, account = pair.partition("=")
        if not re.fullmatch(r"\d{12}", account.strip()):
            raise ValueError("SOURCE_ACCOUNT_IDS deve ser fonte=conta de 12 dígitos")
        out[name.strip()] = account.strip()
    return out


def _leftovers(path: Path, text: str, skip: str = "") -> list[str]:
    lines = [line for line in text.splitlines() if not (skip and line.strip().startswith(skip))]
    code = "\n".join(line.split("#", 1)[0] for line in lines)
    return [f"{path.name}: {p}" for p in PLACEHOLDERS if p in code]


def main(root: Path = Path("."), env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    try:
        accounts = source_accounts(env)
        files = {root / "config.yaml": lambda t: render_config(t, env)}
        for path in sorted((root / "sources").glob("*.yaml")):
            account = accounts.get(path.stem)
            files[path] = (lambda t, a=account: set_value(t, "account_id", f'"{a}"') if a else t)
        missing: list[str] = []
        for path, render in files.items():
            text = render(path.read_bytes().decode("utf-8"))
            path.write_bytes(text.encode("utf-8"))
            missing += _leftovers(path, text, "raw_kms_key_arn:" if env.get("KMS_OPTIONAL") == "1" else "")
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    if missing:
        print(f"::error::valores de exemplo sem secret correspondente: {len(missing)} ({', '.join(missing)})")
        return 1
    print("configuração preenchida")
    return 0


if __name__ == "__main__":
    sys.exit(main())
