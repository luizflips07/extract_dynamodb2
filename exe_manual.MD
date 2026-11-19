# Execução manual — passo a passo (sem GitHub Actions)

Exemplo validado na AWS com a tabela nova **`faturamento`** (4 colunas: `fatura_id`, `cliente_id`, `valor`, `data_emissao`).
Região `us-east-1`. Todos os comandos partem da **raiz do repositório**, com credenciais da conta produtora (`aws configure`).
Valores de conta/bucket: ver [docs/VARIAVEIS.md](docs/VARIAVEIS.md).

## 0. Pré-requisitos (uma vez)
```bash
pip install -r requirements-dev.txt          # boto3, pyyaml, pytest
terraform version                            # >= 1.11 (backend com use_lockfile)
aws sts get-caller-identity                  # confira a conta
```
- Bucket S3 para o state do Terraform já criado (ex.: `<BUCKET_STATE>`, sem `_`).
- `config.yaml` e `sources/local.yaml` com conta, bucket RAW e e-mail corretos.
- `pytest -q` deve passar.

## 1. Definir a tabela no pipeline
Crie `tables/faturamento.yaml` (um arquivo por tabela):
```yaml
source: local
table_name: faturamento
keys: [fatura_id]
columns:
  - { name: fatura_id,    dynamo: S }
  - { name: cliente_id,   dynamo: S }
  - { name: valor,        dynamo: N, type: "decimal(18,2)" }
  - { name: data_emissao, dynamo: S, type: timestamp }
```
Opcional: `partition_by: <coluna>` para particionar o RAW por uma coluna.

## 2. Criar a tabela de origem (mock) e carregar dados
A tabela precisa existir com Streams `NEW_AND_OLD_IMAGES` e PITR. O mock já inclui `faturamento`:
```bash
cd mock/terraform
terraform init
terraform apply                                          # conta nova: cria as 5 tabelas
# Conta onde as outras 4 já existem: crie só a nova
terraform apply -target='aws_dynamodb_table.this["faturamento"]'
cd ../..
python mock/data/load_data.py --tables faturamento       # 200 faturas
```
Confirme: `aws dynamodb describe-table --table-name faturamento --query "Table.[StreamSpecification,ItemCount]"`.

## 3. Data Lake (Glue/RAW) — cria a tabela `raw.dynamodb_faturamento`
```bash
cd infra/datalake
terraform init -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=datalake/raw.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
terraform apply
cd ../..
```
Primeira vez no ambiente: copie `terraform output -raw raw_kms_key_arn` para `datalake.raw_kms_key_arn` do `config.yaml`.
Esperado numa tabela nova: `2 to add` (tabela Glue + assinatura) e a bucket policy atualizada.

## 4. Carga inicial (export PITR → RAW)
```bash
python scripts/initial_load.py --table faturamento --source local --wait    # aguarda COMPLETED
python scripts/load_initial_to_raw.py --table faturamento --source local    # snapshot (eventName=SNAPSHOT) no RAW
```
Saída esperada: `faturamento: 200 itens do export … -> RAW`.

## 5. CDC (Lambda + Event Source Mapping + alarmes)
```bash
cd infra/producer
terraform init -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=producer/local.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
terraform apply -var="source_name=local"
cd ../..
```
Cria, para a tabela: role, Lambda, ESM (lê o stream a partir de `TRIM_HORIZON`), fila SQS de falhas e 5 alarmes (12 recursos).

## 6. View Trusted
```bash
python scripts/apply_trusted_views.py --tables faturamento      # cria trusted.local_faturamento
```

## 7. Teste ponta a ponta
```bash
aws dynamodb put-item --table-name faturamento --item '{"fatura_id":{"S":"F-E2E-1"},"cliente_id":{"S":"C00001"},"valor":{"N":"123.45"},"data_emissao":{"S":"2026-11-19T10:00:00Z"}}'
aws dynamodb update-item --table-name faturamento --key '{"fatura_id":{"S":"F000001"}}' --update-expression "SET valor = :v" --expression-attribute-values '{":v":{"N":"999.99"}}'
aws dynamodb delete-item --table-name faturamento --key '{"fatura_id":{"S":"F000002"}}'
```
Aguarde até ~5 min (janela de batching) e consulte no Athena:
```sql
SELECT eventname, count(*) FROM raw.dynamodb_faturamento WHERE dt >= '<AAAA-MM-DD>' GROUP BY 1;
-- esperado: SNAPSHOT 200, INSERT 201*, MODIFY 1, REMOVE 1
SELECT count(*) FROM trusted.local_faturamento;                                             -- 200 (200 + 1 inserida − 1 removida)
SELECT fatura_id, valor FROM trusted.local_faturamento WHERE fatura_id IN ('F-E2E-1','F000001','F000002');
-- F-E2E-1 = 123.45 | F000001 = 999.99 | F000002 ausente
```
\* o stream ainda retinha as 200 gravações do mock (24 h), por isso o RAW tem o mesmo item como `INSERT` e `SNAPSHOT`; a Trusted deduplica por chave.

## 8. Verificações e problemas comuns
| Sintoma | O que verificar |
|---|---|
| Athena vazio logo após o passo 7 | Aguarde a janela de 5 min; confira `aws lambda list-event-source-mappings --function-name dynodb-produtor-local-faturamento-writer` (State `Enabled`). |
| Export falha por PITR | Habilite PITR na tabela (o mock já habilita) e repita o passo 4. |
| `AccessDenied` no KMS/S3 | Rode o passo 3 depois de criar a tabela no YAML (a bucket policy/CMK é por tabela) e confira `raw_kms_key_arn`. |
| `HIVE_S3_THROTTLING` | Filtre por `dt` e/ou ajuste `datalake.partition_start_date`. |
| Mensagens na fila `…-on-failure` | Lote que esgotou retries; veja os logs da Lambda no CloudWatch. |

## 9. Limpeza (opcional)
```bash
cd mock/terraform && terraform destroy -target='aws_dynamodb_table.this["faturamento"]'   # remove só a tabela de origem
```
Para remover a captura: apague `tables/faturamento.yaml` e rode os passos 3 e 5 (`terraform apply`). Os dados do S3 não são apagados.
