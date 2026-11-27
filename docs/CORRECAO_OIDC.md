# Corrigir `Not authorized to perform sts:AssumeRoleWithWebIdentity`

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

A AWS recusou o token do GitHub porque a condição de confiança da role não bate com ele. Comandos em PowerShell, com credencial da **conta AWS que está falhando**. Troque `<dono>`, `<repositorio>` e `<CONTA>`.

## Passo 1. Conferir a conta

```powershell
aws sts get-caller-identity --query Account --output text
```

## Passo 2. Ver o formato do `sub` do repositório

```powershell
gh api repos/<dono>/<repositorio>/actions/oidc/customization/sub
```

Se aparecer `"use_immutable_subject": true`, copie o `sub_claim_prefix`, no formato `repo:<dono>@<id>/<repositorio>@<id>`. Os IDs são **do repositório**, não da conta AWS: cada repositório (inclusive um clone) tem os seus.

## Passo 3. Ver o `sub` que chegou à AWS

```powershell
aws cloudtrail lookup-events --region us-east-1 --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity --max-results 5 --query "Events[].Username" --output text
```

O evento leva alguns minutos para aparecer. Se não aparecer nenhum, o GitHub está apontando para outra conta ou região: veja o passo 6.

## Passo 4. Ver o que a role aceita

```powershell
aws iam get-role --role-name gha-<project>-datalake --query "Role.AssumeRolePolicyDocument.Statement[0].Condition"
```

```powershell
aws iam get-role --role-name gha-<project>-producer-<fonte> --query "Role.AssumeRolePolicyDocument.Statement[0].Condition"
```

`NoSuchEntity` significa que as roles não foram criadas nesta conta (passo 5A).

O valor aceito precisa ser **idêntico** ao do passo 3:

| Role | `sub` esperado |
|---|---|
| datalake | `<prefixo>:environment:datalake-prod` e `<prefixo>:ref:refs/heads/main` |
| producer | `<prefixo>:environment:producer-<fonte>` |

`<prefixo>` é o `sub_claim_prefix` do passo 2, ou `repo:<dono>/<repositorio>` se o subject não for imutável.

## Passo 5. Corrigir (escolha A ou B)

### 5A. Pelo Terraform (recomendado)

1. Trabalhe numa **cópia** do repositório em que `config.yaml` e `sources/*.yaml` tenham os valores reais da nova conta. Com os valores de exemplo, o Terraform **não cria nenhuma role**.
2. Entre na pasta:

```powershell
cd infra/github_oidc
```

3. Inicie com um bucket de state **desta conta** (não reaproveite o state de outra conta):

```powershell
terraform init -reconfigure -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=github-oidc/oidc.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
```

4. Aplique:

```powershell
terraform apply -var="github_repository=<dono>/<repositorio>" -var="state_bucket=<BUCKET_STATE>" -var="github_subject_prefix=repo:<dono>@<id>/<repositorio>@<id>"
```

Sem subject imutável, omita `github_subject_prefix`. Se a conta já tiver o provedor OIDC do GitHub, a stack o reaproveita sozinha (ver [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md), 2.1).

Numa correção, o plano deve mostrar só as roles alteradas (`0 to add, 2 to change, 0 to destroy`).

### 5B. Pelo console da AWS

1. **IAM → Roles →** `gha-<project>-datalake` **→ Trust relationships → Edit trust policy**.
2. Troque apenas os valores de `token.actions.githubusercontent.com:sub` pelos da tabela do passo 4. Mantenha `aud` igual a `sts.amazonaws.com`. Não use `*`.
3. Salve e repita na role `gha-<project>-producer-<fonte>`.

Um `terraform apply` posterior sem `github_subject_prefix` desfaz a edição manual.

### 5C. Erro no formulário do console: "caracteres que não são válidos: @"

Ao criar uma role com **Identidade da Web** (*Web identity*), os campos como **GitHub organization** não aceitam `@`, então não servem para o subject imutável. Escreva a política de confiança em JSON.

> Para este projeto, prefira o 5A: o Terraform cria também as permissões. O caminho abaixo serve para um repositório qualquer.

1. Obtenha o `sub_claim_prefix` do repositório (passo 2).
2. **IAM → Funções → Criar função → Política de confiança personalizada** (*Custom trust policy*) e cole:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<CONTA>:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:<dono>@<id-do-dono>/<repositorio>@<id-do-repositorio>:ref:refs/heads/main"
      }
    }
  }]
}
```

3. Ajuste o final do `sub` conforme o job do workflow:

| No workflow | Final do `sub` |
|---|---|
| Job com `environment: <nome>` | `:environment:<nome>` |
| Sem `environment`, em push ou disparo manual | `:ref:refs/heads/<branch>` |
| Sem `environment`, em pull request | `:pull_request` |

4. Selecione apenas as permissões que o workflow usa (evite `AdministratorAccess`), dê um nome e crie.
5. Cadastre o ARN da role no secret do repositório.

O provedor `token.actions.githubusercontent.com` precisa existir na conta; ele é único e compartilhado entre repositórios. O workflow precisa de `permissions: id-token: write` e `contents: read`.

**Role já existente:** use **Funções →** a role **→ Relações de confiança → Editar política de confiança**. Colar o JSON substitui a confiança atual: outro repositório que use a mesma role perde o acesso.

## Passo 6. Apontar o GitHub para a nova conta

Os secrets guardam os ARNs das roles. Um repositório faz deploy em uma conta por vez.

```powershell
./scripts/setup_github.ps1 -Repo "<dono>/<repositorio>" -ConfigRoot "<pasta com valores reais>" -StateBucket "<BUCKET_STATE>" -DatalakeRoleArn "arn:aws:iam::<CONTA>:role/gha-<project>-datalake" -ProducerRoleArns @{ local = "arn:aws:iam::<CONTA>:role/gha-<project>-producer-local" }
```

## Passo 7. Testar

```powershell
gh workflow run deploy.yml --repo <dono>/<repositorio> --ref main
```

Aprove o `datalake-prod` na aba *Actions*. A etapa `configure-aws-credentials` precisa passar.

## Se o erro continuar

| Verifique | Detalhe |
|---|---|
| Branch | As roles só aceitam a `main` e os environments |
| Nome do environment | `datalake-prod` e `producer-<fonte>`, com a mesma grafia |
| Provedor OIDC | `aws iam list-open-id-connect-providers` deve listar `token.actions.githubusercontent.com`, com audiência `sts.amazonaws.com` |
| `project` | O nome da role usa o `project` do `config.yaml` |
| Propagação | Espere um minuto após alterar a role |
