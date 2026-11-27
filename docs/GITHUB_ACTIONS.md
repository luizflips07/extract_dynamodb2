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
| `gha-<project>-datalake` | `environment:datalake-prod` | próprio state, buckets RAW e de logs (sem apagar objetos), Glue `raw`/`trusted`, Athena, KMS do projeto, Lambda registradora |
| `gha-<project>-producer-<fonte>` | `environment:producer-<fonte>` | próprio state, Lambda e role writer, ESM, SQS, SNS, alarmes, tabela de parâmetros, export das tabelas marcadas, KMS do projeto |

Todas valem só para o repositório informado em `github_repository`. Nenhuma confia em branch.

**Limites aplicados:**
- **Permissions boundary** (`gha-<project>-boundary`): criar ou alterar uma role `<project>-*` só é permitido se ela estiver presa a ele. O boundary é o teto das Lambdas; as roles de deploy não conseguem alterá-lo.
- **KMS:** só cria chaves já marcadas com `project=<project>` e só administra (e marca) chaves que têm essa tag.
- **State:** cada role lê e grava apenas o próprio arquivo (`<tfstate_prefix>/datalake/raw.tfstate` ou `<tfstate_prefix>/producer/<fonte>.tfstate`) e o lock. Listar o bucket mostra nomes de objetos.
- **Ações:** listadas uma a uma, sem `serviço:*`.

**Tabelas de origem:** o deploy só lê e exporta tabelas DynamoDB com a tag `project=<project>`. Marque cada uma antes do primeiro deploy dela (o `mock/` já marca; se o `project` mudar, use `-var="pipeline_project=<project>"`):

```powershell
aws dynamodb tag-resource --resource-arn arn:aws:dynamodb:<REGIAO>:<CONTA>:table/<tabela> --tags Key=project,Value=<project>
```

A condição por tag depende do ABAC do DynamoDB, ativo por padrão na maioria das contas: confira em *DynamoDB → Settings → Attribute-based access control* (cartão ausente ou `On`).

**Limites que continuam:** quem consegue alterar a `main` altera o código que roda depois da aprovação, e o environment `producer-<fonte>` não pede aprovação. Proteja a branch `main`. O boundary permite às Lambdas ler os streams de qualquer tabela da conta.

## 2. Passo a passo (uma vez)

Comandos em PowerShell. Use uma cópia dos arquivos com `config.yaml` e `sources/` reais.

### 2.1 Criar o provedor e as roles (na pasta `infra/github_oidc`)

Executada **somente à mão**. Precisa de credencial com permissão de IAM, AWS CLI e Python (no Linux/macOS, acrescente `-var="python_command=python3"`).

Antes, veja se o repositório usa o *subject imutável* (padrão em repositórios novos), em que o `sub` do token inclui os IDs do dono e do repositório:

```powershell
gh api repos/<dono>/<repositorio>/actions/oidc/customization/sub
```

Se `use_immutable_subject` for `true`, acrescente ao `apply` abaixo `-var="github_subject_prefix=<valor de sub_claim_prefix>"`.

```powershell
terraform init -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=github-oidc/oidc.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
```

```powershell
terraform apply -var="github_repository=<dono>/<repositorio>" -var="state_bucket=<BUCKET_STATE>"
```

**Provedor já existente:** a conta só aceita um provedor `token.actions.githubusercontent.com`. A stack verifica isso (`find_oidc_provider.py`): se ele não existe, cria; se existe e não é dela, reaproveita e cria só as roles. A saída `oidc_provider` mostra o que aconteceu. O provedor criado pela stack leva a tag `oidc-stack`; um `terraform destroy` dela o apaga, mesmo que outros repositórios o estejam usando. `-var="create_oidc_provider=true"` ou `false` desliga a detecção.

A detecção vale para o provedor, não para as roles: se já houver uma role `gha-<project>-*` com o mesmo nome fora deste state, o `apply` falha. Com contas separadas, aplique em cada uma (outra `key` de state); só as roles daquela conta são criadas.

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

O job `datalake` espera a sua aprovação (*Review deployments*). Quando há tabela nova ou alterada, o job `load` pede uma segunda aprovação.

## 4. Teste realizado (25/11/2026)

Feito com as permissões anteriores, mais amplas. As permissões atuais foram validadas por simulação e **ainda não passaram por um deploy real**.

Ambiente recriado do zero (Data Lake e producer apagados; 5 tabelas do `mock/` recriadas e carregadas) e `deploy` manual:

| Etapa | Resultado |
|---|---|
| `test` | Passou |
| `datalake` | 35 recursos criados |
| `producer (local)` | 47 recursos criados e export das 5 tabelas concluído |
| `load` | Snapshot na RAW e views Trusted recriadas |
| Contagens na Trusted | clientes 500, produtos 100, estoque 100, vendas 100.000, faturamento 200 |
| `MODIFY` e `REMOVE` em `faturamento` | Refletidos na Trusted em cerca de 5 min |
| Testes de integração (`docs/TESTES.md`) | 5 de 5 passaram |

Em 26/11/2026 passaram também o disparo automático por mudança em `tables/*.yaml` e a recriação de tabela com formato alterado ([TESTES.md](TESTES.md), seção 6). Não foi testado mais de uma conta.

**O que fica visível nos logs** (repositório público): conta, bucket RAW, e-mail e ARNs aparecem mascarados (`***`); o nome do bucket de state, que é uma variável, aparece. Cadastre-o como secret se isso importar. O GitHub também avisa que as actions usadas miram o Node.js 20, já obsoleto: atualize as versões quando possível.

## 5. Problemas comuns

| Erro | Causa provável |
|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | O `sub` do token não bate com a role: subject imutável sem `github_subject_prefix` (passo 2.1), ou outro repositório, environment ou branch. O `sub` recebido aparece no CloudTrail, evento `AssumeRoleWithWebIdentity`, campo `userIdentity.userName`. Passo a passo em [CORRECAO_OIDC.md](CORRECAO_OIDC.md) |
| `valores de exemplo sem secret correspondente` | Secret não cadastrado |
| `AccessDenied` no `terraform apply` | Falta uma ação na role: ajuste `infra/github_oidc/main.tf` e reaplique à mão |
| Job parado em *Waiting* | Aprovação do `datalake-prod` |
| `AccessDenied` em `dynamodb:DescribeTable` de uma tabela de origem | Falta a tag `project=<project>` na tabela, ou o ABAC está desligado (seção 1) |
| `AccessDenied` em `iam:CreateRole` ou `iam:PutRolePolicy` | A role precisa do `permissions_boundary`: reaplique `infra/github_oidc` antes do deploy |
| `NoSuchEntity` do boundary num apply manual | Aplique `infra/github_oidc` antes, ou use `-var="use_permissions_boundary=false"` |
