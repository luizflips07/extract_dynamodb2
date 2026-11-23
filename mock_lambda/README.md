# mock_lambda — Lambda que executa o mock (uso MANUAL e LOCAL)

Faz, dentro de uma Lambda, o que a pasta `mock/` faz localmente: **cria as 5 tabelas DynamoDB** de exemplo
(`clientes`, `produtos`, `estoque`, `vendas`, `faturamento`, com Streams `NEW_AND_OLD_IMAGES` + PITR) e
**carrega dados fictícios** (500 / 100 / 100 / 100.000 / 200). Não faz parte do GitHub Actions.

```
mock_lambda/
├── src/handler.py        # código da Lambda (reaproveita mock/data/generate.py)
├── terraform/main.tf     # implanta Lambda + role + log group (state local, execução manual)
├── invoke.py             # invoca a Lambda da sua máquina
└── README.md
```

## Passo a passo
Credenciais da **conta produtora** (`aws configure`), Terraform ≥ 1.6, `pip install boto3`. Na raiz do repositório:

```bash
# 1. Implantar a Lambda (não há build: o zip é montado pelo Terraform com handler.py + mock/data/generate.py)
cd mock_lambda/terraform
terraform init
terraform apply                      # opcional: -var="allowed_account_id=<ID>" -var="table_name_prefix=<prefixo>"
cd ../..

# 2. Executar (tudo: cria as tabelas e carrega os dados)
python mock_lambda/invoke.py

# Variações
python mock_lambda/invoke.py --action create_tables
python mock_lambda/invoke.py --action load_data --tables faturamento --faturamento 500
python mock_lambda/invoke.py --tables clientes vendas --vendas 20000
python mock_lambda/invoke.py --dry-run          # só mostra o payload

# 3. Limpeza
python mock_lambda/invoke.py --action delete_tables --confirm      # apaga as tabelas do mock
cd mock_lambda/terraform && terraform destroy                      # remove a Lambda
```

## Evento aceito pela Lambda
| Campo | Padrão | Descrição |
|---|---|---|
| `action` | `all` | `create_tables`, `load_data`, `all` ou `delete_tables` (exige `confirm: true`) |
| `tables` | todas | subconjunto de `clientes, produtos, estoque, vendas, faturamento` |
| `prefix` | `""` | prefixo dos nomes das tabelas (use o mesmo em `terraform apply -var table_name_prefix=…`, pois a role só acessa tabelas com esse prefixo) |
| `counts` | 500/100/100.000/200 | volumes por tabela (`estoque` = nº de `produtos`) |
| `seed` | 42 | mesmo seed = mesmos dados |

Retorno: por tabela, `create` (`created`/`exists`) e `load` (`items`, `seconds`, `truncated`).

## Observações
- **Timeout:** a Lambda vai até 15 min. Ela para antes do timeout (`truncated: true`); nesse caso invoque de novo com `--action load_data` (as chaves são determinísticas, então regravar é idempotente) ou reduza `--vendas`.
- **Ordem com o pipeline:** carregue os dados **antes** de aplicar `infra/producer` e rodar a carga inicial; depois de ativo, cada item gravado vira evento CDC.
- **Permissões necessárias para o apply:** o usuário que roda o Terraform precisa criar role IAM, log group e Lambda.
- A role da Lambda só acessa tabelas com o prefixo informado (vazio = todas da conta/região).
