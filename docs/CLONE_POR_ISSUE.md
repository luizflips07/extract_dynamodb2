# Clonar o repositório abrindo uma issue

Abrir uma issue pelo modelo **Clonar este repositório** cria um repositório **privado**, na mesma conta do GitHub, com o conteúdo e o histórico da `main`, mais um commit com os parâmetros informados.

**Como criar a issue:** [PASSO_A_PASSO_ISSUE.md](PASSO_A_PASSO_ISSUE.md) (token, campos obrigatórios, uso e problemas comuns).

## Peças

| Arquivo | Papel |
|---|---|
| `.github/ISSUE_TEMPLATE/clonar-repositorio.yml` | Formulário com os 5 campos obrigatórios |
| `.github/workflows/clone.yml` | Cria o repositório, desliga o Actions nele, copia e responde na issue |
| `scripts/clone_request.py` | Valida os campos e ajusta `config.yaml` e `sources/*.yaml` |
| `tests/test_clone_request.py` | Testes da validação e do ajuste |

## O que muda na cópia

```yaml
# config.yaml (exemplo)
project: dynamodb-vendas
tfstate_prefix: "dynamodb-vendas"
region: us-east-1
datalake:
  account_id: "123456789012"
  raw_bucket: "empresa-vendas-raw-001"
  raw_kms_key_arn: "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
```

`sources/*.yaml` recebe o mesmo `account_id`. O id da chave KMS continua de exemplo até o primeiro deploy criar a chave. O state fica em `<bucket de state>/<project>/...`; a "pasta" aparece no S3 no primeiro `terraform apply`, porque o clone não acessa a AWS.

## Segurança

- Só **dono, membros e colaboradores** disparam a automação.
- O corpo da issue é validado campo a campo e chega ao script só por variável de ambiente.
- A conta e o bucket não são escritos no log do workflow, mas **ficam visíveis na própria issue**.
- O `CLONE_TOKEN` cria repositórios: use um token dedicado, com expiração.

## Limites

- Um número de conta vale para o Data Lake e para todas as fontes. Contas separadas: ajuste a cópia depois.
- Os bancos Glue (`raw`, `trusted`) não derivam do projeto: na mesma conta AWS, defina outros na cópia.
- As métricas `DynamoRawIngest` são separadas só por nome de tabela, e um stream DynamoDB aceita no máximo 2 consumidores por shard.
