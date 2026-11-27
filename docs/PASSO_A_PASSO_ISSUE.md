# Passo a passo: criar a issue de clone

Ao final, existe um repositório **privado** novo, com o código deste e os parâmetros informados já gravados. Como a automação funciona: [CLONE_POR_ISSUE.md](CLONE_POR_ISSUE.md).

## Antes de começar (uma vez por repositório)

1. **Issues habilitadas:** *Settings → General → Features → Issues*.
2. **Criar um token** em *Settings (do seu perfil) → Developer settings → Personal access tokens*, com data de expiração:
   - *Fine-grained*: **All repositories**, com **Administration**, **Contents** e **Workflows** em *Read and write*; ou
   - *Classic*: escopos `repo` e `workflow`.
3. **Cadastrar o token** como secret `CLONE_TOKEN`. O comando pede o valor sem mostrá-lo:

```powershell
gh secret set CLONE_TOKEN --repo <dono>/<repositorio>
```

Não cole o token em chat, issue, arquivo ou linha de comando.

## Tenha em mãos

| Campo (todos obrigatórios) | Regra | Exemplo | Onde é gravado na cópia |
|---|---|---|---|
| Nome do novo repositório | letras, números, `.`, `-`, `_` | `extract-dynamodb-vendas` | nome do repositório |
| Nome do projeto | 3 a 30: minúsculas, números, `-`; diferente do atual | `dynamodb-vendas` | `project` e `tfstate_prefix` |
| Região AWS | formato `xx-nome-n` | `us-east-1` | `region` |
| Nome do bucket RAW | 3 a 51: minúsculas, números, `.`, `-`; único no mundo | `empresa-vendas-raw-001` | `datalake.raw_bucket` |
| Número da conta AWS | 12 dígitos | `123456789012` | `datalake.account_id` e `account_id` de `sources/*.yaml` |

> **Visibilidade:** a issue de um repositório público pode ser lida por qualquer pessoa, inclusive a conta e o bucket. Se isso não for aceitável, torne o repositório privado antes. Editar a issue depois não apaga o histórico de edições.

## Pelo site

1. Abra o repositório e vá em **Issues → New issue**.
2. Escolha **Clonar este repositório** e clique em **Get started**.
3. No título, mantenha `[clone]` e complete, ex.: `[clone] vendas`.
4. Preencha os cinco campos. O GitHub não deixa enviar com campo vazio.
5. Clique em **Create**.
6. Em um ou dois minutos, a issue recebe um comentário:
   - **sucesso:** o link do novo repositório, e a issue é fechada;
   - **falha:** o link da execução; abra-o para ver o motivo.

## Pela linha de comando

A issue criada assim precisa ter os mesmos títulos de campo do formulário. Salve em `pedido.md`:

```markdown
### Nome do novo repositório

extract-dynamodb-vendas

### Nome do projeto

dynamodb-vendas

### Região AWS

us-east-1

### Nome do bucket RAW

empresa-vendas-raw-001

### Número da conta AWS

123456789012
```

```powershell
gh issue create --repo <dono>/<repositorio> --title "[clone] vendas" --body-file pedido.md
```

```powershell
gh run list --repo <dono>/<repositorio> --workflow clone.yml --limit 3
```

Para corrigir um pedido, **abra outra issue**: editar não dispara a automação.

## Depois do clone

O repositório novo nasce com o GitHub Actions desligado. Para fazer o primeiro deploy:

1. Conferir `tables/*.yaml` e o e-mail em `config.yaml` (`alarms.notification_emails`).
2. Criar o bucket de state e ter as tabelas DynamoDB com Streams, PITR e a tag `project=<project da cópia>`.
3. Conectar o OIDC ([GITHUB_ACTIONS.md](GITHUB_ACTIONS.md)). O novo repositório tem IDs próprios: ver [CORRECAO_OIDC.md](CORRECAO_OIDC.md).
4. Configurar o repositório com `scripts/setup_github.ps1` (a variável `AWS_REGION` deve ser a região informada).
5. Na mesma conta AWS do original, definir também `datalake.glue_database` e `datalake.trusted_database`.
6. Ligar o Actions em *Settings → Actions → General* e disparar o `deploy`.

## Problemas comuns

| Sintoma | Causa |
|---|---|
| Nada acontece | Título sem `[clone]`, autor sem permissão, ou arquivos fora da `main` |
| `'<campo>' é obrigatório` ou `inválido` | Valor fora da regra; abra outra issue |
| `project ... é o mesmo deste repositório` | Escolha outro nome de projeto |
| `secret CLONE_TOKEN não configurado` | Falta o passo 3 do início |
| `Name already exists` | Já existe um repositório com esse nome |
| `403` ou `refusing to allow ... workflow` | Token sem permissão ou expirado |
