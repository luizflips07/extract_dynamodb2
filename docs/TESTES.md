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
