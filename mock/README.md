# Mock — tabelas DynamoDB e dados fictícios (uso MANUAL)

Cria 4 tabelas de exemplo e as popula, para testar o pipeline CDC quando a conta não tem tabelas.
**Não faz parte do GitHub Actions** (state local, executado à mão). Não é lido pelo `infra/`.

| Tabela | Chave | Itens | Observação |
|---|---|---|---|
| `clientes` | `id` | 500 | `C00001`… |
| `produtos` | `id` | 100 | `P0001`… |
| `estoque` | `produto_id` | 100 | 1 por produto |
| `vendas` | `cliente_id` + `venda_id` | 100.000 | referencia clientes e produtos |

Todas: on-demand, **Streams `NEW_AND_OLD_IMAGES`**, **PITR**, criptografia em repouso. Os atributos casam com `tables/*.yaml`
(o teste `tests/test_mock_generate.py` garante isso).

## Passo a passo
Use as credenciais da **conta produtora** (`sources/local.yaml`).

```bash
# 1. Criar as tabelas
cd mock/terraform
terraform init
terraform apply -var="allowed_account_id=<ID_CONTA_PRODUTORA>"

# 2. Carregar os dados (500 clientes, 100 produtos, 100 estoque, 100.000 vendas)
cd ../..
pip install boto3
python mock/data/load_data.py

# (opcional) testar sem escrever / carga reduzida / só algumas tabelas
python mock/data/load_data.py --dry-run
python mock/data/load_data.py --vendas 1000
python mock/data/load_data.py --tables clientes produtos
```

## Ordem recomendada com o pipeline
1. Criar tabelas e **carregar os dados antes** de aplicar `infra/producer` (os eventos dessa carga ficam 24 h no stream).
2. Rodar a carga inicial: `python scripts/initial_load.py --all --source local`.
3. Aplicar `infra/datalake` e `infra/producer`. Para testar o CDC, altere itens depois (`load_data.py` de novo sobrescreve itens iguais e gera eventos `MODIFY`).

## Custo e limpeza
- 100.000 gravações on-demand custam centavos; PITR e Streams são cobrados por uso/armazenamento.
- Remover tudo: `terraform destroy` em `mock/terraform` (`deletion_protection` está `false` por padrão).
- Os dados são determinísticos (`--seed`, padrão 42): reexecutar não duplica, apenas sobrescreve os mesmos itens.
