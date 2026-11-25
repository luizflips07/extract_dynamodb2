# Deploy pelo GitHub Actions com OIDC

O GitHub entrega ao workflow um token assinado e a AWS troca esse token por credenciais temporárias. **Não há chave de acesso guardada no GitHub.**

| Parte | Onde |
|---|---|
| Provedor OIDC e roles | `infra/github_oidc/` (aplicada à mão) |
| Environments, variáveis e secrets | `scripts/setup_github.ps1` |
| Valores reais dentro do runner | `scripts/ci_config.py` |
| Pipeline | `.github/workflows/deploy.yml` |

## 1. Roles criadas

| Role | Quem assume (`sub` do token) | Alcance |
|---|---|---|
| `gha-<project>-datalake` | `environment:datalake-prod` e `ref:refs/heads/main` | state, buckets RAW e de logs, Glue `raw`/`trusted`, Athena, KMS do projeto, Lambda registradora |
| `gha-<project>-producer-<fonte>` | `environment:producer-<fonte>` | state, Lambda e role writer, ESM, SQS, SNS, alarmes, tabela de parâmetros, export das tabelas, KMS do projeto |

Todas valem só para o repositório informado em `github_repository`.

**Limite conhecido:** as roles de deploy criam roles `<project>-*` com políticas livres e as entregam a Lambdas. Quem consegue alterar a `main` pode, por esse caminho, obter mais permissão na conta. Proteja a branch `main`, mantenha a aprovação manual e, em produção, adicione um *permissions boundary*.

## 2. Passo a passo (uma vez)

Comandos em PowerShell. Use uma cópia dos arquivos com `config.yaml` e `sources/` reais.

### 2.1 Criar o provedor e as roles (na pasta `infra/github_oidc`)

Precisa de credencial com permissão de IAM.

```powershell
terraform init -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=github-oidc/oidc.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
```

```powershell
terraform apply -var="github_repository=<dono>/<repositorio>" -var="state_bucket=<BUCKET_STATE>"
```

Se a conta já tiver o provedor `token.actions.githubusercontent.com`, acrescente `-var="create_oidc_provider=false"`. Com contas separadas, aplique em cada uma (outra `key` de state); só as roles daquela conta são criadas.

### 2.2 Instalar e autenticar a GitHub CLI

```powershell
winget install --id GitHub.cli
```

Abra um novo terminal:

```powershell
gh auth login
```

### 2.3 Configurar o repositório (na raiz)

```powershell
./scripts/setup_github.ps1 -Repo "<dono>/<repositorio>" -ConfigRoot "<pasta com valores reais>" -StateBucket "<BUCKET_STATE>" -DatalakeRoleArn "<output datalake_role_arn>" -ProducerRoleArns @{ local = "<output producer_role_arns.local>" }
```

Ele cria os environments (`datalake-prod` com aprovação sua; todos restritos à `main`) e cadastra:

| Tipo | Nome |
|---|---|
| Variável | `AWS_REGION`, `TFSTATE_BUCKET` |
| Secret | `DATALAKE_ACCOUNT_ID`, `RAW_BUCKET`, `ALARM_EMAILS`, `SOURCE_ACCOUNT_IDS`, `DATALAKE_DEPLOY_ROLE_ARN` |
| Secret do environment `producer-<fonte>` | `PRODUCER_DEPLOY_ROLE_ARN` |

A chave KMS do bucket RAW não é secret: o job `datalake` cria a CMK e passa o id aos jobs seguintes. Assim o primeiro deploy funciona numa conta vazia.

## 3. Executar

- **Automático:** push na `main` que altere `tables/*.yaml`.
- **Manual:** *Actions → deploy → Run workflow* (branch `main`).

O job `datalake` espera a sua aprovação (*Review deployments*).

## 4. Problemas comuns

| Erro | Causa provável |
|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | Repositório, environment ou branch diferentes do `sub` da role. Compare com a condição `token.actions.githubusercontent.com:sub` da role |
| `valores de exemplo sem secret correspondente` | Secret não cadastrado |
| `AccessDenied` no `terraform apply` | Falta uma ação na role: ajuste `infra/github_oidc/main.tf` e reaplique à mão |
| Job parado em *Waiting* | Aprovação do `datalake-prod` |
