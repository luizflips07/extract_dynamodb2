# Clonar o repositório abrindo uma issue

Abrir uma issue pelo modelo **Clonar este repositório** cria um repositório **privado**, na mesma conta, com o conteúdo e o histórico da `main`, mais um commit que grava o `project` escolhido no `config.yaml`.

Arquivos: `.github/ISSUE_TEMPLATE/clonar-repositorio.yml` (formulário), `.github/workflows/clone.yml` (automação) e `scripts/clone_request.py` (validação e ajuste do `config.yaml`).

## 1. Configurar (uma vez)

1. **Issues habilitadas:** *Settings → General → Features → Issues*.
2. **Criar um token** em *Settings (do seu perfil) → Developer settings → Personal access tokens*:
   - *Fine-grained*: acesso **All repositories**, com **Administration**, **Contents** e **Workflows** em *Read and write*; ou
   - *Classic*: escopos `repo` e `workflow`.

   Defina uma data de expiração. Use um token novo, só para isto.
3. **Cadastrar o token** no repositório em *Settings → Secrets and variables → Actions → New repository secret*, com o nome `CLONE_TOKEN`. Nunca coloque o token em arquivo, issue ou chat.

Os arquivos precisam estar na `main`: o GitHub só dispara workflows de issue e mostra o modelo a partir da branch padrão.

## 2. Usar

1. *Issues → New issue → Clonar este repositório*.
2. Mantenha `[clone]` no início do título e informe:
   - **Nome do novo repositório**;
   - **Project**: 3 a 30 caracteres (minúsculas, números, `-`), diferente do atual. Vira o prefixo dos recursos da producer (Lambda, role, fila, tópico SNS, tabela de parâmetros) e a pasta do tfstate.
3. Ao terminar, a issue recebe o link e é fechada. Se falhar, recebe um comentário com o link da execução.

Só **dono, membros e colaboradores** disparam a automação; issues de outras pessoas são ignoradas.

No `config.yaml` da cópia:

```yaml
project: dynamodb-vendas
tfstate_prefix: "dynamodb-vendas"
```

O state fica em `<bucket de state>/dynamodb-vendas/datalake/raw.tfstate` e `.../dynamodb-vendas/producer/<fonte>.tfstate`. A "pasta" aparece no S3 no primeiro `terraform apply`; o clone não acessa a AWS.

## 3. Depois do clone

O novo repositório nasce com o **GitHub Actions desligado**, para a cópia não disparar um deploy. Para usá-lo:

1. Preencher `config.yaml`, `sources/<fonte>.yaml` e `tables/*.yaml`.
2. Criar as variáveis e environments do GitHub (`docs/VARIAVEIS.md`) e ajustar as roles OIDC da AWS para confiar no novo repositório. Nada disso é copiado, nem os secrets.
3. Na **mesma conta AWS**, o `project` e o state já ficam separados, mas **troque também** `datalake.raw_bucket` e defina `datalake.glue_database` / `datalake.trusted_database`: esses nomes não derivam do `project` e já existem no repositório original. As métricas `DynamoRawIngest` são separadas só por nome de tabela, e um stream DynamoDB aceita no máximo 2 consumidores por shard.
4. Ligar o Actions em *Settings → Actions → General*.

## 4. Problemas comuns

| Sintoma | Causa provável |
|---|---|
| Nada acontece | Título sem `[clone]`, autor sem permissão ou workflow ainda fora da `main` |
| `secret CLONE_TOKEN não configurado` | Falta o passo 1.3 |
| `Name already exists` | Escolha outro nome e abra outra issue |
| `403` ou `refusing to allow ... workflow` | Token sem as permissões do passo 1.2 ou expirado |
| Repositório criado, mas vazio | O push falhou: apague o repositório vazio antes de tentar de novo |
