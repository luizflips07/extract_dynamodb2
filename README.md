# DynamoDB → Lambda → S3 RAW (Streams + Lambda por lote) — N fontes, N tabelas

CDC near real-time (~1–5 min) de tabelas DynamoDB para o Data Lake RAW em S3, com carga inicial (snapshot) e views Trusted tipadas.
Passo a passo manual (exemplo `faturamento`): [exe_manual.MD](exe_manual.MD) · mock via Lambda: [mock_lambda/README.md](mock_lambda/README.md) · detalhes técnicos: [docs/DOCUMENTACAO.md](docs/DOCUMENTACAO.md) · **todas as variáveis a alterar (para usar em outra conta), com explicação e local do arquivo: [docs/VARIAVEIS.md](docs/VARIAVEIS.md)** · dados de teste: [mock/README.md](mock/README.md).

## Arquitetura

```
                         ┌────────────────────────── por tabela (tables/*.yaml) ──────────────────────────┐
DynamoDB ── Streams ──►  Event Source Mapping ──► Lambda writer (única) ──► S3 RAW  dynamodb/<fonte>/<tabela>/dt=…/hh=…/*.json.gz
(NEW_AND_OLD_IMAGES,      (batch 10.000 / 300 s,    (NDJSON.gz,       (SSE-KMS, versionado)         │
 24 h, ordem por chave)    bisect, retries)          idempotente)            ▲                       ▼
        │                        │ retries esgotados         │ métricas/logs │ snapshot        Glue  raw.dynamodb_<tabela>
        │ export PITR            ▼                           ▼               │                       │
        └──► <tabela>-initial/  SQS on-failure (1)  CloudWatch ► SNS (alarmes)                       ▼
             (carga inicial) ──► load_initial_to_raw.py ──────┘                     Athena view  trusted.<fonte>_<tabela>
                                                                                    (tipada, deduplicada por chave)
```

- **RAW** é schema-agnóstico: envelope CDC (`eventName`, `Keys`, `NewImage`, `OldImage`, `SequenceNumber`, …) com atributos em DynamoDB JSON.
- **Trusted** é uma view por tabela gerada do schema declarado em `tables/<tabela>.yaml` (tipos convertidos, último evento por chave, `REMOVE` descartado).
- Isolamento por tabela (ESM, alarmes, prefixo S3, tabela Glue) e por fonte (conta, state, CMK, SNS, **Lambda única**, role e **fila on-failure única**).
- **Parâmetros:** a tabela DynamoDB `<projeto>-<fonte>-parametros` espelha `tables/*.yaml` (`nome_tabela`, `contrato`, `data_criacao`, `ultimo_update`).

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `config.yaml` | Região, Data Lake (conta, bucket, CMK, `partition_start_date`), padrões de ESM/Lambda/alarmes, tags |
| `sources/<fonte>.yaml` | Conta produtora (`account_id`, `initial_load`) |
| `tables/<tabela>.yaml` | Tabela capturada: `source`, `table_name`, `keys`, `columns` (+ overrides). Exemplo extra em `tables/_examples/` |
| `src/lambda/handler.py` | Lambda writer **genérica** (uma por fonte, atende todas as tabelas; a config por tabela vem de `tables.json`) |
| `tests/` | Testes unitários (`pytest`) e de integração na AWS (`tests/integration`); como executar em [docs/TESTES.md](docs/TESTES.md) |
| `infra/github_oidc` | Conexão OIDC GitHub → AWS e roles de deploy: [docs/GITHUB_ACTIONS.md](docs/GITHUB_ACTIONS.md) |
| `.github/workflows/clone.yml` | Abrir uma issue `[clone]` cria uma cópia do repositório: [docs/CLONE_POR_ISSUE.md](docs/CLONE_POR_ISSUE.md) |
| `infra/datalake` | Conta Data Lake: bucket RAW, CMK, bucket policy, Glue |
| `infra/producer` | Conta produtora (um state por fonte): CMK, SNS, dashboard, **Lambda única** com role, fila on-failure única, tabela de parâmetros e, por tabela, ESM e alarmes |
| `scripts/` | `initial_load.py`, `load_initial_to_raw.py`, `generate_trusted_ddl.py`, `apply_trusted_views.py` |
| `mock/` | Tabelas e dados fictícios para teste (execução manual, fora do CI) |
| `.github/workflows/` | `validate.yml` (PR) e `deploy.yml` (main, OIDC) |

## Pré-requisitos
- AWS CLI configurado (`aws configure`) na conta produtora, Python 3.12+, Terraform ≥ 1.11 (o backend usa `use_lockfile`).
- Bucket S3 para o state do Terraform (ex.: `tf-state-meu-projeto-001`, mesma região do `config.yaml`).
- `pip install -r requirements-dev.txt`
- Ajustar `config.yaml` e `sources/<fonte>.yaml` (conta, bucket RAW único, e-mail de alarme). Checklist em [docs/VARIAVEIS.md](docs/VARIAVEIS.md).

## Fluxo completo (primeira execução)

> Todos os comandos na raiz do repositório. Região usada: `us-east-1`.

**0. Tabelas de origem** — se ainda não existem, use o mock (cria `clientes`, `produtos`, `estoque`, `vendas`, `faturamento` com Streams `NEW_AND_OLD_IMAGES` + PITR e carrega 500/100/100/100.000/200 itens):
```bash
cd mock/terraform && terraform init && terraform apply && cd ../..
python mock/data/load_data.py
```
Para tabelas reais: habilite Streams `NEW_AND_OLD_IMAGES` e PITR e crie um `tables/<tabela>.yaml` para cada uma.

**1. Data Lake (bucket RAW, CMK, Glue)**
> Se `tfstate_prefix` em `config.yaml` não estiver vazio (cópias criadas por issue), coloque-o antes da `key`: `<tfstate_prefix>/datalake/raw.tfstate` e `<tfstate_prefix>/producer/<fonte>.tfstate`.

```bash
cd infra/datalake
terraform init -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=datalake/raw.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
terraform apply
terraform output -raw raw_kms_key_arn
```
Cole o ARN em `datalake.raw_kms_key_arn` no `config.yaml`. (Se o bucket RAW já existir vazio, importe antes: `terraform import aws_s3_bucket.raw <bucket>`.)

**2. Carga inicial (export PITR → RAW)**
```bash
python scripts/initial_load.py --all --source local          # export FULL para <tabela>-initial/
# aguarde os exports ficarem COMPLETED (aws dynamodb list-exports --table-arn …)
python scripts/load_initial_to_raw.py --all --source local   # snapshot → prefixo lido pelo Glue (eventName=SNAPSHOT, seq 0)
```

**3. CDC (Lambdas + Event Source Mapping + alarmes)**
```bash
cd infra/producer
terraform init -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=producer/local.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
terraform apply -var="source_name=local"
```
O ESM começa em `TRIM_HORIZON` e lê o que o stream ainda retém (24 h). Confirme a assinatura do e-mail no SNS.

**4. Views Trusted**
```bash
python scripts/apply_trusted_views.py
```

**5. Teste ponta a ponta**
```bash
aws dynamodb put-item --table-name clientes --item '{"id":{"S":"E2E-1"},"nome":{"S":"Teste"},"email":{"S":"t@x.com"},"ativo":{"BOOL":true},"criado_em":{"S":"2026-01-01T00:00:00Z"},"cidade":{"S":"Recife"},"uf":{"S":"PE"}}'
aws dynamodb delete-item --table-name clientes --key '{"id":{"S":"E2E-1"}}'
```
Em até ~5 min aparece um arquivo em `s3://<bucket>/dynamodb/local/clientes/dt=…/hh=…/`. No Athena:
```sql
SELECT eventname, count(*) FROM raw.dynamodb_clientes WHERE dt = '<AAAA-MM-DD>' GROUP BY 1;   -- INSERT, REMOVE (+ SNAPSHOT na data do export)
SELECT count(*) FROM trusted.local_clientes WHERE id = 'E2E-1';                              -- 0 (removido)
```

## Operação do dia a dia
- **Nova tabela:** crie `tables/<tabela>.yaml`, abra PR → o deploy cria a infraestrutura; depois rode os passos 2 e 4 para ela (`--table <tabela>`).
- **Nova conta produtora:** crie `sources/<fonte>.yaml` e o environment `producer-<fonte>` no GitHub.
- **Particionar o RAW por uma coluna:** adicione `partition_by: <coluna>` ao `tables/<tabela>.yaml`; o RAW passa a `…/<coluna>=<valor>/dt=…/hh=…/` com uma partição por **valor encontrado** (registradas automaticamente no Glue). Regras e cardinalidade em [docs/VARIAVEIS.md](docs/VARIAVEIS.md#particionamento-do-raw-por-coluna-partition_by); exemplo em `tables/_examples/clientes_particionada.yaml`.
- **Mudou o schema (colunas):** edite `columns` no YAML e rode `apply_trusted_views.py`.
- **Monitoramento:** dashboard e alarmes no CloudWatch (`IteratorAge` é crítico: retenção do stream é 24 h); fila `…-on-failure` não vazia indica lote que esgotou retries.
- **Consultas ao RAW:** sempre filtre por `dt`; `datalake.partition_start_date` limita as partições projetadas (recue se fizer backfill).

## CI/CD (GitHub Actions)
- `validate.yml` (PR): ruff, pytest, geração de DDL, `terraform fmt/validate` e checkov.
- `deploy.yml`: roda **somente** quando `tables/*.yaml` muda (arquivo novo, alterado ou removido) ou manualmente (`workflow_dispatch`). Para cada tabela compara `partition_by` + `keys` + `columns` com a tabela Glue existente: **igual → nada a fazer; diferente (ou nova) → a tabela Glue é apagada/recriada no formato novo e recarregada** (export + snapshot + partições + view Trusted). Dados no S3 não são apagados. Fluxo: testes → datalake (`datalake-prod`, aprovação) → producer por fonte (`producer-<fonte>`) → load. Faça o primeiro apply do datalake à mão (passo 1) para obter o ARN da CMK e versioná-lo no `config.yaml`. Detalhes: `docs/DOCUMENTACAO.md` §15.

## Garantias e limites
- Entrega **at-least-once**; ordem **por chave** dentro do shard (sem ordem global). A Trusted deduplica por chave + `SequenceNumber`; o snapshot (seq 0) perde para qualquer evento real.
- Retenção do stream de 24 h: atraso prolongado = perda de dados → mantenha o alarme `iterator-age` ativo e o e-mail confirmado.
- Bucket policy ≤ 20 KB: ~40–50 tabelas com uma statement por tabela (ver documentação para escalar).
- O RAW pode conter o mesmo item como `INSERT` e `SNAPSHOT` quando o stream ainda retinha a carga inicial.

## Testes
```bash
pytest -q    # Lambda, gerador de DDL, carga do snapshot e gerador do mock
```
Não faz parte dos testes: `terraform plan/apply`, checkov e as consultas Athena (validados manualmente neste ambiente).

## Mapeamento Well-Architected
| Pilar | Práticas |
|---|---|
| Excelência Operacional | IaC declarativa por YAML, CI com validações, deploy em matriz com aprovação, métricas EMF, dashboard |
| Segurança | OIDC, menor privilégio por tabela, CMKs com rotação, TLS obrigatório, Block Public Access, ACL desabilitada, trava de conta |
| Confiabilidade | Escrita idempotente, `ReportBatchItemFailures`, bisect, SQS on-failure, versionamento, alarmes por tabela |
| Performance | Batching, gzip, arm64, tuning por tabela, partition projection |
| Custos | Streams gratuito, poucos PUTs, arm64, lifecycle S3, retenção de logs |
| Sustentabilidade | Serverless, Graviton, compressão, tiering |
