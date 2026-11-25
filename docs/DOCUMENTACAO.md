# Documentação técnica — Ingestão CDC DynamoDB → S3 RAW (N fontes, N tabelas, N schemas)

## 1. Visão geral

CDC near real-time (~1–5 min) de tabelas DynamoDB de uma ou mais **contas produtoras** para o bucket **RAW** do Data Lake (outra conta), em `us-east-1`. Arquitetura: DynamoDB Streams → Event Source Mapping → Lambda writer → S3 RAW.

**Princípio de escala:** *adicionar uma tabela = adicionar um arquivo `tables/<tabela>.yaml`*; *adicionar uma conta produtora = adicionar `sources/<fonte>.yaml`*. Nenhum `.tf` precisa mudar.

### Fluxo
1. A tabela emite um evento por item alterado (`INSERT`/`MODIFY`/`REMOVE`) com `NEW_AND_OLD_IMAGES`.
2. O ESM (um por tabela) agrupa eventos por shard (até 10.000 ou 300 s) e invoca a Lambda writer genérica da fonte.
3. A Lambda grava o lote como um único NDJSON.gz no prefixo da tabela, com SSE-KMS.
4. Falhas: retries + bisect; esgotados, metadados vão à SQS on-failure da tabela.
5. CloudWatch → SNS (on-call). Glue Data Catalog expõe cada tabela RAW (partition projection).
6. A camada **Trusted** é uma view Athena por tabela, tipada e deduplicada, **gerada a partir do schema declarado no YAML**.

## 2. Modelo de segregação

| Nível | Isolamento |
|---|---|
| **Fonte** (conta produtora) | State Terraform próprio (`producer/<fonte>.tfstate`), provider travado na conta (`allowed_account_ids`), CMK e SNS próprios, environment de deploy próprio |
| **Tabela** | ESM (com seus parâmetros de lote/retry), SQS on-failure, 3 alarmes, prefixo S3 `dynamodb/<fonte>/<tabela>/`, tabela Glue `raw.dynamodb_<tabela>`, view `trusted.<fonte>_<tabela>` |
| **Lambda da fonte** | **Uma Lambda writer genérica**, uma role, um log group e 4 alarmes de função, atendendo todas as tabelas da fonte |
| **Data Lake** (conta destino) | Um bucket RAW + CMK; uma statement de bucket policy **por tabela** (a role só escreve no próprio prefixo); uma statement de KMS **por fonte** |

Cada tabela tem ESM, fila e alarmes próprios (falha de lote ou atraso numa tabela não bloqueia o stream das outras), mas **a Lambda é única por fonte**: memória, timeout e concorrência são compartilhados. Nome de recursos: `<projeto>-<fonte>-<tabela>[-sufixo]`; a role `<projeto>-<fonte>-writer` (e a Lambda de mesmo nome) é o contrato entre as duas stacks.

## 3. Schemas diferentes

- **RAW é schema-agnóstico.** Todas as tabelas usam o mesmo envelope: `eventID, eventName, Keys, NewImage, OldImage, SequenceNumber, ApproximateCreationDateTime, SizeBytes, _source, _table, _ingested_at`, com atributos em DynamoDB JSON. Atributo novo na tabela não quebra nada.
- **Schema tipado fica na Trusted.** Cada `tables/<t>.yaml` declara `keys` e `columns` (`dynamo: S|N|BOOL|JSON`, `type` opcional: `timestamp`, `decimal(18,2)`, `bigint`, ...).
- `python scripts/generate_trusted_ddl.py` gera `sql/trusted/<fonte>__<tabela>.sql` com `CREATE OR REPLACE VIEW` que extrai/converte colunas do `NewImage`, particiona pela chave (simples ou composta) e mantém o evento de maior `SequenceNumber`, descartando chaves cujo último evento é `REMOVE`.
- **Evolução de schema:** adicionar coluna = editar `columns` no YAML, regenerar e reaplicar a view. O histórico no RAW é preservado.
- `tables/_examples/pedidos.yaml` mostra outro schema (chave composta, decimal, JSON). Fica fora de `tables/*.yaml`, portanto não é implantado.

## 4. Estrutura do repositório

| Caminho | Descrição |
|---|---|
| `config.yaml` | Global: projeto, ambiente, região, Data Lake (conta, bucket, CMK), padrões de ESM/Lambda/alarmes, tags |
| `sources/<fonte>.yaml` | `name`, `account_id`, `initial_load`, opcional `initial_load_role_arns` |
| `tables/<tabela>.yaml` | `source`, `table_name`, `glue_table`, overrides opcionais (`streaming`, `lambda`, `alarms`), `keys`, `columns` |
| `src/lambda/handler.py` | Lambda writer **genérica** (única por fonte; descobre a tabela pelo `eventSourceARN` e lê a config em `tables.json`, gerado pelo Terraform) |
| `infra/producer/` | Stack por fonte: CMK, SNS, dashboard e `module "table"` (for_each) |
| `infra/producer/main.tf` | Lambda única, role, SNS, CMK e, por tabela: ESM, SQS on-failure e alarmes |
| `infra/datalake/` | `main.tf` (leitura dos YAMLs), `kms.tf`, `s3.tf` (bucket, policy), `glue.tf` |
| `scripts/initial_load.py` | Export FULL PITR (`--table` ou `--all [--source]`) |
| `scripts/generate_trusted_ddl.py` | Gera as views Trusted dos schemas |
| `tests/` | Testes da Lambda e do gerador de DDL |
| `.github/workflows/` | `validate.yml` (PR) e `deploy.yml` (main) |

## 5. Lambda writer
- Python 3.12, arm64, só `boto3`. Um lote = um objeto S3, chave determinística `dynamodb/<fonte>/<tabela>/dt=YYYY-MM-DD/hh=HH/<seq_ini>-<seq_fim>.json.gz` (retry sobrescreve o mesmo objeto).
- Escrita com `aws:kms` (CMK do Data Lake), Bucket Key, checksum SHA256, `bucket-owner-full-control`.
- Erro no `PutObject` → `batchItemFailures` com o primeiro `SequenceNumber`.
- Logs JSON e métricas EMF (`DynamoRawIngest`: `RecordsWritten`, `BytesWritten`, `BatchFailed`, dimensão `Table`).
- **Genérica:** uma função atende N tabelas. O ESM de cada tabela entrega registros com `eventSourceARN`; a Lambda extrai o nome da tabela e busca `{raw_prefix, partition_by}` em `tables.json` (empacotado no zip pelo Terraform a partir de `tables/*.yaml` da fonte; `TABLES_CONFIG` na env sobrescreve, para testes). Tabela fora da config: lote reportado como falha (vai para a SQS), sem gravar.
- Variáveis de ambiente (comuns): `RAW_BUCKET`, `RAW_KMS_KEY_ARN`, `SOURCE_NAME`, `LOG_LEVEL`.
- Métricas EMF por tabela (`DynamoRawIngest`, dimensão `Table`): `RecordsWritten`, `BytesWritten`, `BatchFailed` e `StreamLagMs` (atraso evento→RAW; a `IteratorAge` da AWS é só por função).

## 6. Infraestrutura

### 6.1 Stack `producer` (um state por fonte; `-var source_name=<fonte>`)
Lê `sources/<fonte>.yaml` e todas as `tables/*.yaml` com `source == <fonte>`. Cria CMK com rotação, tópico SNS (e-mails de `config.yaml`), dashboard consolidado, **uma Lambda genérica com role e log group** e, por tabela, um ESM e uma fila on-failure.

Role única de menor privilégio: leitura dos streams **das tabelas da fonte**, `PutObject` só nos prefixos delas, as duas CMKs, as filas on-failure, logs e X-Ray. Lambda com X-Ray, log group com KMS e retenção; preconditions: Streams `NEW_AND_OLD_IMAGES` em todas as tabelas e nenhum `lambda:` por tabela. ESM por tabela (`TRIM_HORIZON`, `ReportBatchItemFailures`, bisect, retries, idade máx. 24 h, destino de falha SQS). Alarmes: da função (`iterator-age`, `errors`, `throttles`, `duration-p99`) e por tabela (`stream-lag`, `batch-failed`, `on-failure-not-empty`).
Parâmetros por tabela: `merge(config.yaml, override do YAML da tabela)`.

### 6.2 Stack `datalake` (um state para todo o lake)
Lê todas as fontes e tabelas. Cria: CMK (rotação; uma statement por fonte liberando `role/<projeto>-<fonte>-writer` e, opcional, `initial_load_role_arns`); bucket RAW (Block Public Access, versionamento, `BucketOwnerEnforced`, SSE-KMS, access logs, lifecycle IA → Glacier IR); bucket policy (TLS obrigatório, nega chave KMS diferente, **uma statement por tabela** restringindo a role ao prefixo, export inicial por fonte em `<tabela>-initial/`); bancos Glue `raw` e `trusted`; **uma tabela Glue por tabela** com partition projection (`dt`, `hh`).

## 7. CI/CD
- **validate.yml (PR):** ruff, pytest (inclui validação de schema de todas as tabelas), geração de DDL, `terraform fmt/validate` e checkov nas duas stacks.
- **deploy.yml (main):** testes e descoberta das fontes → apply **datalake** (environment `datalake-prod`) → apply **producer em matriz, uma execução por fonte** (environment `producer-<fonte>`). OIDC, state remoto S3 com lock/criptografia.
- Configurar: variáveis `TFSTATE_BUCKET` e `DATALAKE_DEPLOY_ROLE_ARN`; em cada environment `producer-<fonte>`, `PRODUCER_DEPLOY_ROLE_ARN` da conta correspondente.

## 8. Procedimentos

**Nova tabela**
1. Na tabela: Streams `NEW_AND_OLD_IMAGES` + PITR.
2. Criar `tables/<tabela>.yaml` (source, nome, keys, columns) e abrir PR.
3. Após o deploy: `python scripts/initial_load.py --table <tabela>` (idealmente antes do ESM habilitar) e `python scripts/generate_trusted_ddl.py`, aplicando o SQL no Athena.

**Nova fonte (conta)**
1. Criar `sources/<fonte>.yaml`; criar o environment `producer-<fonte>` no GitHub com a role OIDC da conta.
2. Criar as `tables/*.yaml` apontando para `source: <fonte>`.

**Implantação inicial:** ajustar `TODO` de `config.yaml`/`sources`; aplicar datalake, copiar `raw_kms_key_arn` para `config.yaml`; aplicar producer. Se a policy acusar principal inexistente (role ainda não criada), aplicar a producer e reaplicar a datalake.

## 9. Operação
- Latência: janela de batching (60–300 s) + invocação.
- `iterator-age` é crítico: o stream retém 24 h (fixo); atraso prolongado = perda de dados → recuperar com novo export PITR e deduplicar na Trusted. Confirme a assinatura do SNS.
- Fila on-failure não vazia: ver logs (`put_object_failed`); mensagens trazem faixa de sequence para reprocesso.
- Throughput por tabela: `streaming.*` no YAML da tabela (`parallelization_factor` máx. 10; máx. 2 consumidores por shard).

## 10. Garantias e limites
- At-least-once: retry não duplica, mas o *bisect* gera lotes com chaves distintas → a view Trusted deduplica por chave + `SequenceNumber`.
- Ordem garantida por chave dentro do shard; não há ordem global entre shards/tabelas.
- **Escala:** bucket policy ≤ 20 KB (~40–50 tabelas por statement individual). Acima disso, trocar as statements por tabela por uma por fonte com `ArnLike` no padrão do nome da role (perde-se a restrição por prefixo na policy; mantida só pela IAM da role). Limites de contas Lambda/ESM e de recursos CloudWatch por conta também se aplicam.
- Muitos arquivos pequenos em tabelas de baixo volume: compactação (Glue/CTAS) opcional.
- **Lambda única por fonte** (decisão): menos recursos e um ponto de deploy; em troca, memória/timeout/concorrência reservada são comuns, a role enxerga todas as tabelas da fonte e `Errors`/`Duration`/`Throttles` da AWS são agregados (atraso e falhas por tabela vêm das métricas EMF).

## 11. Well-Architected
| Pilar | Práticas |
|---|---|
| Excelência Operacional | IaC declarativa por YAML, CI com validações e checkov, deploy em matriz com aprovação, métricas EMF, dashboard |
| Segurança | OIDC, menor privilégio por tabela, CMKs com rotação, TLS, Block Public Access, ACL desabilitada, trava de conta, isolamento por prefixo |
| Confiabilidade | Escrita idempotente, `ReportBatchItemFailures`, bisect, SQS on-failure, versionamento, alarmes por tabela, blast radius limitado |
| Performance | Batching, gzip, arm64, tuning por tabela, partition projection |
| Custos | Streams gratuito, poucos PUTs, arm64, lifecycle, retenção de logs |
| Sustentabilidade | Serverless, Graviton, compressão, tiering |

## 12. Status de validação
| Item | Resultado |
|---|---|
| pytest (Lambda + gerador de DDL) | 8/8 passando |
| `terraform fmt -check` / `validate` (producer e datalake) | OK |
| `terraform plan/apply`, checkov, ruff, execução das views no Athena, teste ponta a ponta | Não executados |

## 13. Carga inicial no RAW (snapshot)
1. `python scripts/initial_load.py --all --source local` exporta cada tabela (PITR) para `dynamodb/<fonte>/<tabela>-initial/` (DynamoDB JSON; a tabela Glue **não** lê esse prefixo).
2. `python scripts/load_initial_to_raw.py --all --source local` converte o export mais recente para o envelope do CDC e grava no prefixo lido pelo Glue: `eventName = SNAPSHOT`, `SequenceNumber = "0"`. Idempotente.
3. A view Trusted deduplica por chave: qualquer evento real do CDC (sequência maior) prevalece sobre o snapshot. Se o stream ainda retinha os INSERTs da carga, o RAW terá `INSERT` e `SNAPSHOT` para o mesmo item (esperado).

Notas de implementação descobertas no teste ponta a ponta:
- O JsonSerDe da tabela RAW converte **todas** as chaves JSON para minúsculas (`{"id":{"s":"..."}}`); o gerador de views usa paths minúsculos e `CAST(... AS timestamp)` (views não aceitam `timestamp with time zone`).
- `datalake.partition_start_date` (config.yaml) limita as partições projetadas; sem isso, consultas sem filtro de `dt` listam milhares de prefixos e sofrem `HIVE_S3_THROTTLING`. Recue a data se fizer backfill. Sempre filtre por `dt` em consultas ao RAW.

## 14. Particionamento opcional do RAW (`partition_by`)
- Declarado em `tables/<tabela>.yaml` (`partition_by`). Parametros e regras: `docs/VARIAVEIS.md` (seção 3).
- **Lambda:** `partition_by` (campo da tabela em `tables.json`, só preenchido quando a tabela declara a coluna) faz o lote ser dividido por valor; um objeto por valor em `<prefixo>/<coluna>=<valor>/dt=…/hh=…/<seq>-<seq>.json.gz`. A lógica de valor/normalização está em `src/lambda/partitioning.py`, **compartilhada** com `scripts/load_initial_to_raw.py` para que o snapshot use o mesmo caminho.
- **Glue (`infra/datalake/glue.tf`):** a coluna vira a 1ª partição (`[<coluna>, dt, hh]`) e a tabela deixa de usar *partition projection*: as partições são registradas dinamicamente por valor encontrado (Lambda `<project>-partition-registrar`, `infra/datalake/partition_registrar.tf`, acionada por S3 → EventBridge a cada arquivo novo; `src/registrar/handler.py`). *Preconditions* validam que a coluna existe em `columns`, que não usa nome reservado e que `keys` estão em `columns`.
- **Trusted:** a view é criada normalmente para tabelas particionadas.
- Sem `partition_by`, nada muda (mesmo layout, mesma tabela Glue).

## 15. Deploy orientado a mudança em `tables/` (GitHub Actions)
- **Gatilho:** `push` na `main` somente com mudança em `tables/*.yaml` (arquivo novo, alterado ou removido; `tables/_examples/` e demais pastas não disparam). `workflow_dispatch` permite execução manual (ex.: publicar mudanças de `infra/` ou `src/`).
- **Formato da tabela** = `partition_by` + `keys` + `columns` (assinatura SHA-256, calculada igual em `infra/datalake/main.tf` e `scripts/table_sync.py`). O Terraform grava a assinatura na tabela Glue (`dynalambda.signature`).
- **Job `datalake`:** `table_sync.py plan` compara cada YAML com a tabela Glue → `new` / `changed` / `unchanged`; depois `terraform apply`. **Igual → nada é feito.** **Diferente → a tabela Glue é apagada e recriada** (`terraform_data` + `replace_triggered_by`). Os arquivos do S3 **não são apagados**.
- **Job `producer`** (por fonte): `terraform apply` e, só para tabelas `new`/`changed` da fonte, `initial_load.py --wait` (export PITR).
- **Job `load`:** só para `new`/`changed`: `load_initial_to_raw.py` (snapshot), `MSCK REPAIR TABLE` (se `partition_by`) e recriação da view Trusted.
- **Remover um `tables/<t>.yaml`** faz o Terraform destruir ESM, fila, alarmes e tabela Glue dessa tabela (a Lambda única é atualizada sem ela) (dados no S3 permanecem).
- **Snapshots repetidos:** como cada recriação gera um novo export, a Trusted desempata `SNAPSHOT`s pela data do export (`approximatecreationdatetime`); eventos reais do CDC sempre prevalecem (sequência > 0).
