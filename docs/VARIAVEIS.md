# Variáveis e valores a alterar (guia de migração para outra conta)

Tudo o que é específico de conta, região, nome ou ambiente está listado aqui, com **o que faz** e **em qual arquivo está**.
O que não aparece nesta lista não precisa ser alterado para implantar em outra conta.

Legenda de obrigatoriedade: **Obrigatório** (deploy falha ou vai para o lugar errado), **Ajustar** (funciona, mas são valores de exemplo), **Opcional** (tem padrão).

---

## 1. `config.yaml` (raiz do repositório) — configuração global

Lido pelo Terraform das duas stacks (`yamldecode`) e pelos scripts em `scripts/`.

| Variável | Obrig. | Para que serve | Valor atual |
|---|---|---|---|
| `project` | Ajustar | Prefixo do nome de **todos** os recursos (`<project>-<fonte>-<tabela>`: Lambdas, roles, filas, alarmes, SNS). Faz parte do contrato de nome da role entre as duas stacks: se mudar, mude antes de criar os recursos. | `dynamodb-produtor` |
| `environment` | Ajustar | Tag `environment` em todos os recursos (`dev`/`hml`/`prod`). | `prod` |
| `region` | **Obrigatório** | Região AWS das duas stacks e dos scripts. **Deve ser a região das tabelas DynamoDB** (o stream só é lido na mesma região). | `us-east-1` |
| `datalake.account_id` | **Obrigatório** | ID da conta do Data Lake (dona do bucket RAW). O provider da stack `datalake` é travado nessa conta (`allowed_account_ids`) e o export inicial usa como `S3BucketOwner`. | `111111111111` (placeholder) |
| `tfstate_prefix` | Opcional | Pasta das chaves de state no bucket. Vazio mantém as chaves na raiz. O clone por issue preenche com o `project`. **Não mude depois do 1º apply** sem migrar o state (`terraform init -migrate-state`). | `""` |
| `datalake.raw_bucket` | **Obrigatório** | Nome do bucket RAW. Precisa ser **único no mundo** e só pode ter minúsculas, números, `-` e `.` (sem `_`). O bucket de logs é `<raw_bucket>-access-logs`. | `meu-datalake-raw-001` (placeholder) |
| `datalake.raw_kms_key_arn` | **Obrigatório** | ARN da CMK que criptografa o RAW. Só existe depois do 1º apply da stack `datalake`: copie o output `raw_kms_key_arn` (`terraform output -raw raw_kms_key_arn`). A Lambda usa para gravar e o IAM aponta para ela. | `arn:aws:kms:us-east-1:111111111111:key/0000…` (placeholder) |
| `datalake.partition_start_date` | Ajustar | Primeiro dia (`AAAA-MM-DD`) que o Athena considera nas partições `dt`. Limita a quantidade de partições listadas (sem isso há `HIVE_S3_THROTTLING`). Recue se for fazer backfill de dados antigos. Padrão se omitido: `2024-01-01`. | `2026-11-01` |
| `datalake.glue_database` | Opcional | Banco Glue das tabelas RAW. | `raw` (padrão) |
| `datalake.trusted_database` | Opcional | Banco Glue das views Trusted. | `trusted` (padrão) |
| `datalake.athena_results` | Opcional | Destino dos resultados de consulta usado por `scripts/apply_trusted_views.py` (`s3://bucket/prefixo/`). Padrão: `s3://<raw_bucket>-access-logs/athena-results/`. | (não definido) |
| `streaming.batch_size` | Opcional | Máx. de registros por invocação (até 10.000). | `10000` |
| `streaming.max_batching_window_seconds` | Opcional | Janela de acumulação do lote (0–300 s). Define a latência do dado no RAW (~janela + segundos). | `300` |
| `streaming.parallelization_factor` | Opcional | Lotes simultâneos por shard (1–10). Mantém a ordem por chave. | `1` |
| `streaming.max_retry_attempts` | Opcional | Retries antes de enviar o lote para a SQS on-failure (única por fonte). | `5` |
| `streaming.max_record_age_seconds` | Opcional | Idade máxima de um registro do stream (retenção do stream é 24 h = 86400). | `86400` |
| `streaming.bisect_batch_on_error` | Opcional | Divide o lote ao falhar para isolar o registro ruim. | `true` |
| `lambda.runtime` / `lambda.architecture` | Opcional | Runtime e arquitetura (`arm64` é mais barato). | `python3.12` / `arm64` |
| `lambda.memory_mb` / `lambda.timeout_seconds` | Opcional | Memória e timeout da Lambda writer **única da fonte** (valem para todas as tabelas). | `512` / `120` |
| `lambda.reserved_concurrency` | Opcional | Concorrência reservada (`-1` = sem reserva). Use para proteger a conta. | `-1` |
| `lambda.log_retention_days` | Opcional | Retenção dos logs da Lambda. | `90` |
| `alarms.iterator_age_ms` | Ajustar | Limite do alarme de atraso de leitura do stream (ms). Crítico: perto de 24 h = perda de dados. | `300000` (5 min) |
| `alarms.errors_threshold` / `throttles_threshold` | Opcional | Limites dos alarmes de erro e throttling. | `1` / `1` |
| `alarms.duration_p99_ms` | Opcional | Limite do alarme de duração p99 (deve ficar abaixo do timeout). | `100000` |
| `alarms.notification_emails` | **Obrigatório** | Lista de e-mails inscritos no SNS de alarmes. Cada pessoa precisa **confirmar a assinatura** no e-mail recebido. | `["seu-email@exemplo.com"]` (placeholder) |
| `tags.owner` / `tags.cost-center` / `tags.data-classification` | Ajustar | Tags de governança/custo em todos os recursos. | `squad-dados` / `1234` / `confidential` |

Os blocos `streaming` e `alarms` podem ser **sobrescritos por tabela** em `tables/<tabela>.yaml`. O bloco `lambda` **não**: a Lambda é única por fonte e o Terraform recusa `lambda:` em tabela.

## 2. `sources/<fonte>.yaml` — uma conta produtora por arquivo

O nome do arquivo (sem `.yaml`) é o identificador da fonte, usado em nomes de recursos, state e environment do GitHub.

| Variável | Obrig. | Para que serve | Valor atual |
|---|---|---|---|
| `name` | **Obrigatório** | Nome da fonte (igual ao nome do arquivo). Entra no prefixo S3 `dynamodb/<fonte>/…` e nos nomes dos recursos. | `local` |
| `account_id` | **Obrigatório** | Conta AWS **onde estão as tabelas DynamoDB** e onde roda a stack `producer`. O provider é travado nessa conta; a stack `datalake` usa para montar o ARN da role e liberar bucket/CMK. | `222222222222` (placeholder) |
| `initial_load` | Opcional | `true` = `scripts/initial_load.py` faz o export FULL dessa fonte. | `true` |
| `initial_load_role_arns` | Opcional | Roles/usuários (da conta produtora) autorizados na CMK do RAW para o export inicial, além das roles `*-writer`. Preencha se quem executa o export não for uma role `<project>-<fonte>-*-writer` e não tiver permissão via IAM. | (não definido) |

Para **adicionar outra conta produtora**: novo arquivo em `sources/` + environment `producer-<fonte>` no GitHub (seção 4).

## 3. `tables/<tabela>.yaml` — uma tabela por arquivo

Cada arquivo em `tables/*.yaml` vira Lambda, role, ESM, fila, alarmes, prefixo S3 e tabela Glue. Exemplos fora do deploy em `tables/_examples/`.

| Variável | Obrig. | Para que serve |
|---|---|---|
| `source` | **Obrigatório** | Fonte dona da tabela (nome de um arquivo de `sources/`). |
| `table_name` | **Obrigatório** | Nome **exato** da tabela DynamoDB (precisa ter Streams `NEW_AND_OLD_IMAGES` e PITR). |
| `glue_table` | Opcional | Nome da tabela Glue do RAW. Padrão: `dynamodb_<table_name>`. |
| `keys` | **Obrigatório** | Atributos da chave primária (partição [+ ordenação]). Usado na deduplicação da Trusted e no snapshot. Devem estar em `columns`. |
| `columns` | **Obrigatório** | Schema lógico: `name`, `dynamo` (`S`/`N`/`BOOL`/`JSON`) e `type` opcional (`timestamp`, `decimal(18,2)`, `int`, …). Gera a view Trusted. |
| **`partition_by`** | Opcional | **Coluna pela qual o RAW é particionado** (ver abaixo). Deve estar em `columns`, ser `S`, `N` ou `BOOL` e não pode ser `dt`/`hh` nem nome do envelope. |
| `streaming` / `alarms` | Opcional | Sobrescrevem os padrões do `config.yaml` só para esta tabela (ex.: `streaming: { batch_size: 5000 }`). `lambda:` por tabela não é aceito (Lambda única por fonte). |

### Particionamento do RAW por coluna (`partition_by`)

```yaml
partition_by: uf
```

- **Layout no S3:** sem `partition_by`: `dynamodb/<fonte>/<tabela>/dt=…/hh=…/`; com: `dynamodb/<fonte>/<tabela>/<coluna>=<valor>/dt=…/hh=…/`. A coluna vira partição da tabela Glue (`raw.dynamodb_<tabela>`), além de `dt`/`hh`.
- **Como funciona:** a Lambda separa cada lote por valor da coluna e grava **um arquivo por valor**. Em eventos `REMOVE` usa a `OldImage`. O mesmo vale para o snapshot da carga inicial.
- **Valor normalizado:** caracteres fora de `[A-Za-z0-9._-]` viram `_` (`São Paulo` → `S_o_Paulo`); ausente/vazio → `__NULL__`. O valor original continua intacto em `NewImage`/`OldImage`.
- **Valores dinâmicos:** não há lista de valores. Cada valor encontrado nos registros vira uma partição; a Lambda `<project>-partition-registrar` (no Data Lake, acionada por evento do S3 via EventBridge) registra no Glue a partição `<coluna>=<valor>/dt=/hh=` de cada arquivo novo (idempotente). Consultas sem filtro funcionam e a view Trusted é criada normalmente. Novos valores aparecem em segundos após o arquivo ser gravado.
- **Cardinalidade:** prefira colunas com poucas dezenas de valores. Muitos valores = muitos arquivos pequenos por lote.
- **Mudança depois de existir dados:** vale só para dados gravados daí em diante; os arquivos antigos continuam no layout anterior e **não** são lidos pela nova definição da tabela. Reprocesse (snapshot/backfill) se precisar de histórico no novo layout.
- **Se o valor da coluna mudar** (`MODIFY`), o evento vai para a partição do **novo** valor; a Trusted mantém o último evento por chave, então o estado atual continua correto.
- Exemplo completo: `tables/_examples/clientes_particionada.yaml`.

## 4. GitHub Actions (Settings → Secrets and variables → Actions)

Usados em `.github/workflows/deploy.yml`. Passo a passo (OIDC, roles e script de cadastro) em [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md).

| Nome | Tipo / escopo | Para que serve |
|---|---|---|
| `AWS_REGION` | Variável do repositório | Região das credenciais e do backend. Igual a `region` do `config.yaml`. |
| `TFSTATE_BUCKET` | Variável do repositório | Bucket do state do Terraform. |
| `DATALAKE_DEPLOY_ROLE_ARN` | Secret do repositório | Role OIDC da conta do Data Lake. |
| `PRODUCER_DEPLOY_ROLE_ARN` | Secret do environment `producer-<fonte>` | Role OIDC da conta produtora. |
| `DATALAKE_ACCOUNT_ID`, `RAW_BUCKET`, `ALARM_EMAILS`, `SOURCE_ACCOUNT_IDS` | Secrets do repositório | Substituem os valores de exemplo de `config.yaml` e `sources/` dentro do runner (`scripts/ci_config.py`). |

Nomes fixos no código (altere em `deploy.yml` se quiser outros):
- Environments: `datalake-prod` (configure aprovação manual) e `producer-<fonte>` (criar um por arquivo de `sources/`).
- Chaves de state: `<tfstate_prefix>/datalake/raw.tfstate` e `<tfstate_prefix>/producer/<fonte>.tfstate` (`tfstate_prefix` vem do `config.yaml`; vazio = sem pasta; o `deploy.yml` aplica sozinho).
- Versão do Terraform: `terraform_version: "1.11.4"` em `deploy.yml` e `validate.yml` (mínimo 1.11, por causa de `use_lockfile`).

## 5. Execução manual (sem GitHub Actions)

| Valor | Onde informar | Para que serve |
|---|---|---|
| `-backend-config="bucket=…"`, `"key=…"`, `"region=…"` | `terraform init` em `infra/datalake` e `infra/producer` | Aponta o state remoto (bucket de state, chave do state, região). Veja o passo a passo no `README.md`. |
| `-var="source_name=<fonte>"` | `terraform apply` em `infra/producer` | Escolhe qual fonte (`sources/<fonte>.yaml`) a stack aplica. Obrigatório. |
| Credenciais/região do AWS CLI | `aws configure` (ou `AWS_PROFILE`/`AWS_REGION`) | Os scripts em `scripts/` e `mock/data/` usam a conta/região configuradas; os de `scripts/` usam a `region` do `config.yaml`. |

## 6. Mock (apenas para teste) — `mock/`

Não é usado em produção; só se for recriar os dados de exemplo na nova conta.

| Variável | Arquivo | Para que serve | Padrão |
|---|---|---|---|
| `region` | `mock/terraform/main.tf` | Região onde as tabelas mock são criadas. **Igual a `region` do `config.yaml`.** | `us-east-1` |
| `allowed_account_id` | `mock/terraform/main.tf` | Trava o apply na conta certa (`-var="allowed_account_id=<ID>"`). | `null` (sem trava) |
| `table_name_prefix` | `mock/terraform/main.tf` | Prefixo dos nomes das tabelas. Deixe vazio para casar com `tables/*.yaml`. | `""` |
| `deletion_protection` | `mock/terraform/main.tf` | Proteção contra exclusão das tabelas mock. | `false` |
| `--region`, `--prefix`, `--clientes/--produtos/--vendas`, `--seed` | `mock/data/load_data.py` | Região (padrão: a do `aws configure`), prefixo de tabela e volumes da carga fictícia. | região do CLI / 500 / 100 / 100000 / 42 |

## 7. Valores fixos no código (altere só se precisar de política diferente)

| Valor | Arquivo | O que controla |
|---|---|---|
| Transições S3 (30 d → `STANDARD_IA`, 180 d → `GLACIER_IR`), expiração de versões antigas (30 d), limpeza de multipart (7 d), logs de acesso (365 d) | `infra/datalake/s3.tf` | Ciclo de vida/custo do bucket RAW e do bucket de logs. |
| Retenção da fila on-failure (14 dias), visibilidade (300 s) | `infra/producer/main.tf` | Quanto tempo os lotes com falha ficam disponíveis para reprocesso. |
| Janela das alarmes (300 s, 1 período) | `infra/producer/main.tf` | Sensibilidade dos alarmes CloudWatch. |
| `starting_position = "TRIM_HORIZON"` | `infra/producer/main.tf` | Onde o ESM começa a ler ao ser criado (início do stream, até 24 h). |
| Faixa de `hh` 0–23 e fuso UTC | `infra/datalake/glue.tf`, `src/lambda/handler.py` | Partições horárias em UTC. |
| Versões dos providers (`aws ~> 5.60`, `archive ~> 2.4`) | `infra/*/main.tf` | Compatibilidade do Terraform. |

## 8. Pré-requisitos a criar na nova conta (fora do repositório)

1. **Bucket de state** (`TFSTATE_BUCKET`) na região do projeto.
2. **Provedor OIDC do GitHub** e **roles de deploy**: criados por `infra/github_oidc` ([GITHUB_ACTIONS.md](GITHUB_ACTIONS.md)).
3. **Tabelas DynamoDB** com Streams `NEW_AND_OLD_IMAGES` e PITR (para testes, `mock/` cria).

## 9. Checklist de migração (ordem)

1. Editar `config.yaml`: `region`, `datalake.account_id`, `datalake.raw_bucket`, `alarms.notification_emails`, tags (e `project`/`environment`, se quiser).
2. Editar/criar `sources/<fonte>.yaml` (`name`, `account_id`) e `tables/<tabela>.yaml` (`source`, `table_name`, `keys`, `columns`, `partition_by` se quiser).
3. Criar os pré-requisitos da seção 8 e cadastrar variáveis e secrets no GitHub (`scripts/setup_github.ps1`).
4. Pelo GitHub Actions: disparar o `deploy`. Os passos 1 e 2 podem ser feitos numa cópia local só para o `setup_github.ps1`, sem commitar valores reais.
5. À mão (sem Actions): aplicar `infra/datalake`, copiar `raw_kms_key_arn` para `config.yaml` e seguir o `README.md`.
