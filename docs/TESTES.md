# Testes

Há dois grupos:

| Grupo | Onde | Usa AWS? | Quando roda |
|---|---|---|---|
| Unitários | `tests/test_*.py` | Não (S3/SQS simulados) | `pytest` local e no GitHub Actions |
| Integração | `tests/integration/test_aws.py` | **Sim** | Só manualmente, com `RUN_AWS_TESTS=1` |

Todos os comandos abaixo são PowerShell, executados na **raiz do repositório**.

## 1. Preparar (uma vez)

```powershell
python -m pip install -r requirements-dev.txt
```

## 2. Testes unitários

Não precisam de credencial AWS.

```powershell
python -m pytest -q
```

Os de integração aparecem como `s` (pulados), o que é o esperado.

| Arquivo | O que garante |
|---|---|
| `test_handler.py` | Chave S3 determinística, NDJSON.gz, falha parcial, roteamento por tabela, tabela não configurada, aviso na SQS com parâmetros (uma vez por lote) e fila indisponível |
| `test_partitioning.py` | Particionamento do RAW por coluna (`partition_by`) |
| `test_generate_ddl.py` | Views Trusted e schema de todos os `tables/*.yaml` |
| `test_load_initial.py` | Carga inicial (snapshot) |
| `test_table_sync.py` | Detecção de tabela nova/alterada no deploy e registro de partições |
| `test_mock_generate.py`, `test_mock_lambda.py` | Dados fictícios e a Lambda de mock |

Rodar um só arquivo ou um só teste:

```powershell
python -m pytest tests/test_handler.py -v
```

```powershell
python -m pytest -k parameters -v
```

## 3. Testes de integração (AWS)

### 3.1 Pré-requisitos
- `infra/datalake` e `infra/producer` aplicados.
- `config.yaml` e `sources/<fonte>.yaml` com os **valores reais** (os mesmos usados no apply). Com os valores de exemplo, o teste para de propósito.
- `tables/*.yaml` iguais aos que foram aplicados.
- Credencial da **conta produtora** configurada (`aws configure` ou `$env:AWS_PROFILE`).
- Permissões: `dynamodb:Scan/DescribeTable/PutItem/DeleteItem`, `lambda:ListEventSourceMappings/InvokeFunction`, `sqs:GetQueueUrl/GetQueueAttributes/ReceiveMessage/DeleteMessage/ChangeMessageVisibility`, `kms:Decrypt` nas CMKs e, no fluxo, `s3:ListBucket/GetObject` no bucket RAW.

Conferir a conta antes:

```powershell
aws sts get-caller-identity
```

### 3.2 Parâmetros, recursos e falhas forçadas (cerca de 1 minuto)

```powershell
$env:RUN_AWS_TESTS = "1"
```

```powershell
python -m pytest tests/integration -v
```

| Teste | O que faz | O que deve acontecer |
|---|---|---|
| `test_parameter_table_mirrors_yaml_files` | Lê `<projeto>-<fonte>-parametros` | Um item por YAML, `contrato` igual ao arquivo, `data_criacao` ≤ `ultimo_update` |
| `test_glue_tables_match_the_yaml_format` | Compara cada YAML com a tabela Glue (precisa de `glue:GetTable`, na conta do Data Lake) | Todas `unchanged`: o formato aplicado é o do repositório |
| `test_every_stream_mapping_is_enabled_and_uses_the_single_queue` | Lista os mapeamentos da Lambda | Um por tabela, `Enabled`, destino de falha = fila única |
| `test_failed_batch_sends_table_parameters_to_the_queue` | Invoca a Lambda com um lote falso cuja chave o S3 recusa | `batchItemFailures` e mensagem na fila com tabela, erro e parâmetros |
| `test_unconfigured_table_is_reported_without_parameters` | Invoca com uma tabela inexistente na configuração | Mensagem com `parametros: null` |

**Efeitos colaterais:** nada é gravado na RAW. Cada teste apaga apenas a própria mensagem da fila. O alarme `<projeto>-<fonte>-<tabela>-batch-failed` (e possivelmente o da fila) **dispara e envia e-mail**, voltando ao normal sozinho em alguns minutos. Em ambiente produtivo, avise o plantão antes.

### 3.3 Fluxo completo até a RAW (alguns minutos)

```powershell
$env:RUN_AWS_FLOW = "1"
```

```powershell
python -m pytest tests/integration -v -k reaches_raw
```

Grava um item `TESTE-<número>` na tabela (só as chaves), espera o arquivo aparecer na RAW (janela de `streaming.max_batching_window_seconds` + 180 s) e apaga o item. O `INSERT` e o `REMOVE` ficam no histórico da RAW; a view Trusted não mostra o item depois que o `REMOVE` chega.

Opções:

| Variável | Padrão | Uso |
|---|---|---|
| `TEST_SOURCE` | `local` | Outra fonte (`sources/<nome>.yaml`) |
| `TEST_TABLE` | primeira tabela da fonte | Tabela usada na falha e no fluxo, ex.: `$env:TEST_TABLE = "faturamento"` |
| `AWS_DATALAKE_PROFILE` | credencial atual | Perfil AWS para ler a RAW quando o bucket está em outra conta |

Ao terminar, desligue:

```powershell
Remove-Item Env:RUN_AWS_TESTS, Env:RUN_AWS_FLOW -ErrorAction SilentlyContinue
```

## 4. Conferências manuais (sem pytest)

Os nomes abaixo usam `project: dynamodb-produtor` e a fonte `local`.

Tabela de parâmetros:

```powershell
aws dynamodb scan --table-name dynamodb-produtor-local-parametros --query "Items[].[nome_tabela.S,data_criacao.S,ultimo_update.S]" --output table
```

Contrato de uma tabela:

```powershell
aws dynamodb get-item --table-name dynamodb-produtor-local-parametros --key '{\"nome_tabela\":{\"S\":\"faturamento\"}}' --query Item.contrato.S --output text
```

Mensagens na fila (a leitura não apaga; a mensagem volta em 30 s):

```powershell
aws sqs receive-message --queue-url (aws sqs get-queue-url --queue-name dynamodb-produtor-local-on-failure --query QueueUrl --output text) --max-number-of-messages 10 --message-attribute-names All
```

**`ultimo_update` ao mudar um YAML:** altere um arquivo em `tables/` e rode o plan (na pasta `infra/producer`, já com `terraform init`). Devem aparecer apenas `aws_dynamodb_table_item.parametros["<tabela>"]` e `terraform_data.ultimo_update["<tabela>"]`; `data_criacao` não muda. Se a mudança for em `keys`, `columns`, `partition_by` ou `streaming`, a Lambda e o mapeamento também aparecem.

```powershell
terraform plan -var="source_name=local"
```

## 5. Não coberto

- Mensagem de **lote descartado** enviada pela própria AWS (exige esgotar 5 retries ou 24 h num stream real).
- Execução do `deploy.yml` no GitHub Actions (resultado do teste manual em [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md)).
- Consulta à view Trusted (ver `exe_manual.MD`).
- Automação de clone por issue (só a leitura do formulário tem teste unitário).

## 6. Testes do pipeline no GitHub (manuais)

Precisam de `gh auth login` e do repositório configurado ([GITHUB_ACTIONS.md](GITHUB_ACTIONS.md)).

### Resultados (26/11/2026)

| Teste | Resultado | Evidência |
|---|---|---|
| `validate.yml` em PR | **Reprovado** | `ruff` e `checkov` falharam no [PR 1](https://github.com/luizflips07/extract_dynamodb2/pull/1). O `ruff` passou após o `ruff.toml`; o `checkov` continua falhando (6.4) |
| Disparo automático por `tables/*.yaml` | Passou | O merge do PR 1 iniciou o `deploy` com evento `push` |
| Recriação por formato alterado (`estoque.quantidade`: `int` → `bigint`) | Passou | Só `estoque` saiu como `changed`; tabela Glue substituída, export e view só dela; Trusted com 100 linhas e coluna `bigint`; arquivos antigos mantidos na RAW; só o `ultimo_update` de `estoque` mudou |
| Clone por issue | Passou | Com o `CLONE_TOKEN` ([issue 5](https://github.com/luizflips07/extract_dynamodb2/issues/5), dados fictícios): repositório privado criado com a `main` e um commit com os parâmetros em `config.yaml` e `sources/local.yaml`; Actions desligado, nenhuma execução, secrets não copiados; issue respondida e fechada. Recusa de campos inválidos na [issue 4](https://github.com/luizflips07/extract_dynamodb2/issues/4). Não testado: deploy a partir da cópia |

### 6.1 `validate.yml` em um PR

```powershell
git switch -c teste/validate
```

Altere algo em `src/`, `tests/`, `scripts/`, `infra/` ou `tables/`, faça o commit e:

```powershell
git push -u origin teste/validate
```

```powershell
gh pr create --fill
```

```powershell
gh pr checks --watch
```

O job `lambda-and-schemas` deve passar (regras fixadas em `ruff.toml`). O job `terraform` roda `fmt`, `validate` e `checkov`, e hoje falha no `checkov`.

### 6.2 Disparo automático e recriação por formato alterado

Os dois são testados juntos: mude o `type` de uma coluna (ex.: em `tables/estoque.yaml`, `quantidade` de `int` para `bigint`) e leve a mudança à `main`.

```powershell
gh run list --workflow deploy.yml --limit 3
```

Esperado:
- uma execução com evento `push`, sem disparo manual;
- após a aprovação do `datalake-prod`, o log mostra `estoque changed` e as demais `unchanged`;
- a tabela Glue `raw.dynamodb_estoque` é recriada, há um novo export só de `estoque` e o job `load` recria a view;
- os arquivos antigos no S3 continuam lá e a contagem em `trusted.local_estoque` não muda.

Depois, confira o formato:

```powershell
$env:RUN_AWS_TESTS = "1"
```

```powershell
python -m pytest tests/integration -v -k glue
```

### 6.3 Clone por issue

Cadastre o secret (o comando pede o valor sem mostrá-lo; não cole o token em chat, arquivo ou issue):

```powershell
gh secret set CLONE_TOKEN --repo <dono>/<repositorio>
```

Abra a issue pelo modelo **Clonar este repositório** (*Issues → New issue*) e acompanhe:

```powershell
gh run list --workflow clone.yml --limit 3
```

Esperado: repositório privado criado, com o Actions desligado, os parâmetros gravados em `config.yaml` e `sources/`, e a issue fechada com o link. Ver [PASSO_A_PASSO_ISSUE.md](PASSO_A_PASSO_ISSUE.md).

### 6.4 Achados do `checkov` em aberto

Nenhuma verificação foi desligada. Para cada item, decidir entre corrigir e registrar a exceção (`#checkov:skip=<ID>:<motivo>`).

| ID | Assunto | Onde |
|---|---|---|
| `CKV_AWS_109`, `111`, `356` | Política de chave KMS ampla | `kms` (datalake), `kms_app` (producer) |
| `CKV_AWS_116`, `117`, `272` | Lambda sem DLQ, VPC e assinatura de código | `writer`, `registrar` |
| `CKV_AWS_50`, `115`, `158`, `173` | X-Ray, concorrência reservada, KMS no log e nas variáveis | `registrar` |
| `CKV_AWS_144` | Replicação entre regiões | buckets RAW e de logs |
| `CKV_AWS_21`, `145`, `CKV2_AWS_62`, `65` | Versionamento, KMS, notificações e ACL | bucket de logs |
| `CKV_AWS_300` | Abortar uploads incompletos | lifecycle dos buckets |

O `checkov` lê `infra/datalake` e `infra/producer`; `infra/github_oidc` não é verificada.
