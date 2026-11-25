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
Cria a **Lambda única** da fonte (`dynamodb-produtor-local-writer`, com role e log group; ela recebe a configuração de todas as tabelas em `tables.json`) e, para cada tabela, o ESM (lê o stream a partir de `TRIM_HORIZON`), a fila SQS de falhas e 3 alarmes. Tabela nova = só ESM, fila e alarmes, mais a atualização do `tables.json` da mesma Lambda. Os outputs mostram função, role e, por tabela, ESM, fila, prefixo RAW e alarmes (`terraform output tables`).

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
| Athena vazio logo após o passo 7 | Aguarde a janela de 5 min; confira `aws lambda list-event-source-mappings --function-name dynamodb-produtor-local-writer` (State `Enabled`). |
| Export falha por PITR | Habilite PITR na tabela (o mock já habilita) e repita o passo 4. |
| `AccessDenied` no KMS/S3 | Rode o passo 3 depois de criar a tabela no YAML (a bucket policy/CMK é por tabela) e confira `raw_kms_key_arn`. |
| `HIVE_S3_THROTTLING` | Filtre por `dt` e/ou ajuste `datalake.partition_start_date`. |
| Mensagens na fila `…-on-failure` | Lote que esgotou retries; veja os logs da Lambda no CloudWatch. |

## 9. Limpeza (opcional)
```bash
cd mock/terraform && terraform destroy -target='aws_dynamodb_table.this["faturamento"]'   # remove só a tabela de origem
```
Para remover a captura: apague `tables/faturamento.yaml` e rode os passos 3 e 5 (`terraform apply`). Os dados do S3 não são apagados.

## 10. Alternativa ao passo 2: criar e carregar o mock por uma Lambda
A pasta `mock_lambda/` faz, dentro de uma Lambda, o mesmo que o passo 2 (criar as tabelas do mock e carregar os dados). É manual e local, e também fora do GitHub Actions. Detalhes em [mock_lambda/README.md](mock_lambda/README.md).

**Como funciona**
- `mock_lambda/terraform` implanta a Lambda `mock-dynamodb-loader` (Python 3.12, arm64, timeout 15 min), a role e o log group. O zip é montado pelo Terraform com `mock_lambda/src/handler.py` + `mock/data/generate.py`.
- `mock_lambda/invoke.py` roda na sua máquina e invoca a Lambda, passando ação, tabelas e volumes.
- A Lambda cria cada tabela se ela não existir (on-demand, Streams `NEW_AND_OLD_IMAGES`, PITR) e grava os itens em lotes paralelos. Rodar de novo é seguro: tabela existente retorna `exists` e as chaves dos itens são determinísticas.
- Ela para antes do timeout e devolve `truncated: true`. Nesse caso invoque de novo com `--action load_data`.
- Ações: `create_tables`, `load_data`, `all` (padrão) e `delete_tables` (exige `--confirm`).

**Passo a passo**
```bash
cd mock_lambda/terraform
terraform init
terraform apply                       # opcional: -var="table_name_prefix=<prefixo>" restringe a role a esse prefixo
cd ../..
python mock_lambda/invoke.py                                                  # cria as 5 tabelas e carrega 500/100/100/100.000/200
python mock_lambda/invoke.py --action load_data --tables faturamento --faturamento 500
```
Depois siga do passo 3 (Data Lake) deste documento.

**Limpeza**
```bash
python mock_lambda/invoke.py --action delete_tables --confirm      # apaga as tabelas do mock
cd mock_lambda/terraform && terraform destroy                      # remove a Lambda
```

**Testado na AWS:** com `--prefix lt_` e volumes pequenos, criou as 5 tabelas com Streams e PITR, carregou os itens (50/10/10/500/30), devolveu `exists` na repetição, recusou `delete_tables` sem `--confirm`, apagou com `--confirm`, e o `terraform destroy` removeu os 4 recursos. A carga completa de 100.000 vendas dentro dos 15 min não foi medida.
