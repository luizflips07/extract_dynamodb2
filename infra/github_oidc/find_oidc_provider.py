"""Decide se a stack deve criar o provedor OIDC do GitHub ou reaproveitar o que já existe.

Chamado pelo Terraform (data "external"): lê um JSON na entrada e responde {"create": "true"|"false"}.
Só faz leitura no IAM, pela AWS CLI, com as mesmas credenciais do ambiente.

O provedor é único por conta. A stack só o "cria" se ele não existe ou se foi ela mesma que o criou
(sem isso, o 2º apply veria o provedor existente e o apagaria do state e da conta).
"""
import json
import subprocess
import sys

HOST = "token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
OWNER_TAG = "oidc-stack"


def aws(*args):
    res = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"falha na AWS CLI ({' '.join(args[:2])}): {res.stderr.strip()}")
    return json.loads(res.stdout)


def is_ours(tags, owner, project):
    if OWNER_TAG in tags:
        return tags[OWNER_TAG] == owner
    # provedor criado por esta stack antes da tag oidc-stack existir
    legacy = {"managed-by": "terraform", "layer": "ci", "project": project}
    return all(tags.get(k) == v for k, v in legacy.items())


def decide(provider, owner, project):
    """provider: resposta de get-open-id-connect-provider, ou None se não existe. True = criar."""
    if provider is None:
        return True
    tags = {t["Key"]: t["Value"] for t in provider.get("Tags", [])}
    if is_ours(tags, owner, project):
        return True
    if AUDIENCE not in provider.get("ClientIDList", []):
        sys.exit(
            f"o provedor {HOST} já existe, mas sem o público {AUDIENCE}. Ele não é desta stack; acrescente com: "
            f"aws iam add-client-id-to-open-id-connect-provider --client-id {AUDIENCE} "
            "--open-id-connect-provider-arn <arn do provedor>"
        )
    return False


def main():
    query = json.load(sys.stdin)
    listed = aws("iam", "list-open-id-connect-providers")["OpenIDConnectProviderList"]
    arn = next((p["Arn"] for p in listed if p["Arn"].endswith(f":oidc-provider/{HOST}")), None)
    provider = aws("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", arn) if arn else None
    print(json.dumps({"create": "true" if decide(provider, query["owner"], query["project"]) else "false"}))


if __name__ == "__main__":
    main()
