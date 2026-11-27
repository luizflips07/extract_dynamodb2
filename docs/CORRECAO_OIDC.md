# Corrigir `Not authorized to perform sts:AssumeRoleWithWebIdentity`

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

A AWS recusou o token do GitHub porque a condição de confiança da role não bate com ele. Comandos em PowerShell, com credencial da **conta AWS que está falhando**. Troque `<dono>`, `<repositorio>` e `<CONTA>`.

## Passo 1. Conferir a conta

```powershell
aws sts get-caller-identity --query Account --output text
```

## Passo 2. Ver o formato do `sub` do repositório

```powershell
gh api repos/<dono>/<repositorio>/actions/oidc/customization/sub
```

Se aparecer `"use_immutable_subject": true`, copie o `sub_claim_prefix`, no formato `repo:<dono>@<id>/<repositorio>@<id>`. Os IDs são **do repositório**, não da conta AWS: cada repositório (inclusive um clone) tem os seus.

## Passo 3. Ver o `sub` que chegou à AWS

```powershell
aws cloudtrail lookup-events --region us-east-1 --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity --max-results 5 --query "Events[].Username" --output text
```

O evento leva alguns minutos para aparecer. Se não aparecer nenhum, o GitHub está apontando para outra conta ou região: veja o passo 6.

## Passo 4. Ver o que a role aceita

```powershell
aws iam get-role --role-name gha-<project>-datalake --query "Role.AssumeRolePolicyDocument.Statement[0].Condition"
```

```powershell
aws iam get-role --role-name gha-<project>-producer-<fonte> --query "Role.AssumeRolePolicyDocument.Statement[0].Condition"
```

`NoSuchEntity` significa que as roles não foram criadas nesta conta (passo 5A).

O valor aceito precisa ser **idêntico** ao do passo 3:

| Role | `sub` esperado |
|---|---|
| datalake | `<prefixo>:environment:datalake-prod` |
| producer | `<prefixo>:environment:producer-<fonte>` |

`<prefixo>` é o `sub_claim_prefix` do passo 2, ou `repo:<dono>/<repositorio>` se o subject não for imutável.

## Passo 5. Corrigir (escolha A ou B)

### 5A. Pelo Terraform (recomendado)

1. Trabalhe numa **cópia** do repositório em que `config.yaml` e `sources/*.yaml` tenham os valores reais da nova conta. Com os valores de exemplo, o Terraform **não cria nenhuma role**.
2. Entre na pasta:

```powershell
cd infra/github_oidc
```

3. Inicie com um bucket de state **desta conta** (não reaproveite o state de outra conta):

```powershell
terraform init -reconfigure -backend-config="bucket=<BUCKET_STATE>" -backend-config="key=github-oidc/oidc.tfstate" -backend-config="region=us-east-1" -backend-config="encrypt=true" -backend-config="use_lockfile=true"
```

4. Aplique:

```powershell
terraform apply -var="github_repository=<dono>/<repositorio>" -var="state_bucket=<BUCKET_STATE>" -var="github_subject_prefix=repo:<dono>@<id>/<repositorio>@<id>"
```

Sem subject imutável, omita `github_subject_prefix`. Se a conta já tiver o provedor OIDC do GitHub, a stack o reaproveita sozinha (ver [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md), 2.1).

Numa correção só do `sub`, o plano deve mostrar apenas as roles alteradas (`0 to add, 2 to change, 0 to destroy`).

### 5B. Pelo console da AWS

1. **IAM → Roles →** `gha-<project>-datalake` **→ Trust relationships → Edit trust policy**.
2. Troque apenas os valores de `token.actions.githubusercontent.com:sub` pelos da tabela do passo 4. Mantenha `aud` igual a `sts.amazonaws.com`. Não use `*`.
3. Salve e repita na role `gha-<project>-producer-<fonte>`.

Um `terraform apply` posterior sem `github_subject_prefix` desfaz a edição manual.

### 5C. Erro no formulário do console: "caracteres que não são válidos: @"

Ao criar uma role com **Identidade da Web** (*Web identity*), os campos como **GitHub organization** não aceitam `@`, então não servem para o subject imutável. Escreva a política de confiança em JSON.

> Para este projeto, prefira o 5A: o Terraform cria também as permissões. O caminho abaixo serve para um repositório qualquer.

1. Obtenha o `sub_claim_prefix` do repositório (passo 2).
2. **IAM → Funções → Criar função → Política de confiança personalizada** (*Custom trust policy*) e cole:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<CONTA>:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:<dono>@<id-do-dono>/<repositorio>@<id-do-repositorio>:ref:refs/heads/main"
      }
    }
  }]
}
```

3. Ajuste o final do `sub` conforme o job do workflow:

| No workflow | Final do `sub` |
|---|---|
| Job com `environment: <nome>` | `:environment:<nome>` |
| Sem `environment`, em push ou disparo manual | `:ref:refs/heads/<branch>` |
| Sem `environment`, em pull request | `:pull_request` |

4. Selecione apenas as permissões que o workflow usa (evite `AdministratorAccess`), dê um nome e crie. As permissões deste projeto estão no [anexo](#anexo-as-roles-completas).
5. Cadastre o ARN da role no secret do repositório.

O provedor `token.actions.githubusercontent.com` precisa existir na conta; ele é único e compartilhado entre repositórios. O workflow precisa de `permissions: id-token: write` e `contents: read`.

**Role já existente:** use **Funções →** a role **→ Relações de confiança → Editar política de confiança**. Colar o JSON substitui a confiança atual: outro repositório que use a mesma role perde o acesso.

## Passo 6. Apontar o GitHub para a nova conta

Os secrets guardam os ARNs das roles. Um repositório faz deploy em uma conta por vez.

```powershell
./scripts/setup_github.ps1 -Repo "<dono>/<repositorio>" -ConfigRoot "<pasta com valores reais>" -StateBucket "<BUCKET_STATE>" -DatalakeRoleArn "arn:aws:iam::<CONTA>:role/gha-<project>-datalake" -ProducerRoleArns @{ local = "arn:aws:iam::<CONTA>:role/gha-<project>-producer-local" }
```

## Passo 7. Testar

```powershell
gh workflow run deploy.yml --repo <dono>/<repositorio> --ref main
```

Aprove o `datalake-prod` na aba *Actions*. A etapa `configure-aws-credentials` precisa passar.

## Se o erro continuar

| Verifique | Detalhe |
|---|---|
| Environment | As roles só aceitam os environments, e eles só aceitam a `main` |
| Nome do environment | `datalake-prod` e `producer-<fonte>`, com a mesma grafia |
| Provedor OIDC | `aws iam list-open-id-connect-providers` deve listar `token.actions.githubusercontent.com`, com audiência `sts.amazonaws.com` |
| `project` | O nome da role usa o `project` do `config.yaml` |
| Propagação | Espere um minuto após alterar a role |

## Anexo: as roles completas

Cópia genérica do que `infra/github_oidc` cria, para conferência ou criação pelo console. A fonte da verdade é o Terraform: se ele mudar, atualize este anexo.

| Marcador | Valor |
|---|---|
| `<CONTA>` | número da conta AWS |
| `<REGIAO>` | região, ex.: `us-east-1` |
| `<project>` | `project` do `config.yaml` |
| `<fonte>` | nome do arquivo em `sources/` |
| `<BUCKET_RAW>` | `datalake.raw_bucket` |
| `<BUCKET_STATE>` | bucket do state do Terraform |
| `<dono>`, `<repositorio>`, `<id-...>` | do `sub_claim_prefix` (passo 2) |

Os bancos Glue aparecem como `raw` e `trusted`, e o `tfstate_prefix` vazio; troque se o `config.yaml` usar outros.

Pelo console: crie primeiro a política gerenciada `gha-<project>-boundary`; depois a role com a relação de confiança (5C) e **Adicionar permissões → Criar política em linha → JSON**.

### Boundary: `gha-<project>-boundary`

Política gerenciada. Não é anexada às roles de deploy: é o teto das roles que elas criam.

```json
{
  "Statement": [
    {
      "Action": [
        "dynamodb:ListStreams",
        "dynamodb:GetShardIterator",
        "dynamodb:GetRecords",
        "dynamodb:DescribeStream"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:dynamodb:<REGIAO>:<CONTA>:table/*/stream/*",
      "Sid": "ReadStreams"
    },
    {
      "Action": "dynamodb:GetItem",
      "Effect": "Allow",
      "Resource": "arn:aws:dynamodb:<REGIAO>:<CONTA>:table/<project>-*",
      "Sid": "ReadParameters"
    },
    {
      "Action": "s3:PutObject",
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_RAW>/dynamodb/*",
      "Sid": "WriteRaw"
    },
    {
      "Action": [
        "kms:GenerateDataKey",
        "kms:Encrypt",
        "kms:DescribeKey"
      ],
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "EncryptOnly"
    },
    {
      "Action": "kms:Decrypt",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/project": "<project>"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:kms:*:*:key/*",
      "Sid": "DecryptProjectKeys"
    },
    {
      "Action": "sqs:SendMessage",
      "Effect": "Allow",
      "Resource": "arn:aws:sqs:<REGIAO>:<CONTA>:<project>-*",
      "Sid": "OnFailureQueue"
    },
    {
      "Action": [
        "logs:PutLogEvents",
        "logs:CreateLogStream"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:logs:<REGIAO>:<CONTA>:log-group:/aws/lambda/<project>-*",
      "Sid": "WriteLogs"
    },
    {
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords"
      ],
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "XRay"
    },
    {
      "Action": [
        "glue:GetTable",
        "glue:CreatePartition",
        "glue:BatchCreatePartition"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:glue:<REGIAO>:<CONTA>:table/raw/*",
        "arn:aws:glue:<REGIAO>:<CONTA>:database/raw",
        "arn:aws:glue:<REGIAO>:<CONTA>:catalog"
      ],
      "Sid": "RegisterPartitions"
    }
  ],
  "Version": "2012-10-17"
}
```

### Role do Data Lake: `gha-<project>-datalake`

**Relação de confiança**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<CONTA>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:<dono>@<id-do-dono>/<repositorio>@<id-do-repositorio>:environment:datalake-prod"
        }
      }
    }
  ]
}
```

**Política de permissões** (em linha, nome `deploy`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_STATE>",
      "Sid": "StateList"
    },
    {
      "Action": [
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_STATE>/datalake/raw.tfstate",
      "Sid": "StateObject"
    },
    {
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_STATE>/datalake/raw.tfstate.tflock",
      "Sid": "StateLock"
    },
    {
      "Action": "kms:CreateKey",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/project": "<project>"
        }
      },
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "KmsCreateTagged"
    },
    {
      "Action": "kms:ListAliases",
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "KmsList"
    },
    {
      "Action": [
        "kms:UpdateAlias",
        "kms:DeleteAlias",
        "kms:CreateAlias"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:kms:<REGIAO>:<CONTA>:alias/<BUCKET_RAW>",
        "arn:aws:kms:<REGIAO>:<CONTA>:alias/<project>-*"
      ],
      "Sid": "KmsAliases"
    },
    {
      "Action": [
        "kms:UpdateKeyDescription",
        "kms:UpdateAlias",
        "kms:UntagResource",
        "kms:TagResource",
        "kms:ScheduleKeyDeletion",
        "kms:RetireGrant",
        "kms:ReEncryptTo",
        "kms:ReEncryptFrom",
        "kms:PutKeyPolicy",
        "kms:ListResourceTags",
        "kms:ListGrants",
        "kms:GetKeyRotationStatus",
        "kms:GetKeyPolicy",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:GenerateDataKey",
        "kms:Encrypt",
        "kms:EnableKeyRotation",
        "kms:EnableKey",
        "kms:DescribeKey",
        "kms:DeleteAlias",
        "kms:Decrypt",
        "kms:CreateGrant",
        "kms:CreateAlias"
      ],
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/project": "<project>"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:kms:*:*:key/*",
      "Sid": "KmsProjectKeys"
    },
    {
      "Action": [
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePolicy",
        "iam:CreateRole"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PermissionsBoundary": "arn:aws:iam::<CONTA>:policy/gha-<project>-boundary"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:iam::<CONTA>:role/<project>-*",
      "Sid": "ProjectRolesWithBoundary"
    },
    {
      "Action": [
        "iam:UpdateRole",
        "iam:UntagRole",
        "iam:TagRole",
        "iam:ListRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:ListAttachedRolePolicies",
        "iam:GetRolePolicy",
        "iam:GetRole",
        "iam:DeleteRole"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:iam::<CONTA>:role/<project>-*",
      "Sid": "ProjectRoles"
    },
    {
      "Action": "iam:PassRole",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "lambda.amazonaws.com"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:iam::<CONTA>:role/<project>-*",
      "Sid": "PassOnlyToLambda"
    },
    {
      "Action": [
        "lambda:UpdateFunctionConfiguration",
        "lambda:UpdateFunctionCode",
        "lambda:UntagResource",
        "lambda:TagResource",
        "lambda:PutFunctionConcurrency",
        "lambda:ListVersionsByFunction",
        "lambda:ListTags",
        "lambda:GetFunctionConfiguration",
        "lambda:GetFunctionCodeSigningConfig",
        "lambda:GetFunction",
        "lambda:DeleteFunctionConcurrency",
        "lambda:DeleteFunction",
        "lambda:CreateFunction"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:lambda:<REGIAO>:<CONTA>:function:<project>-*",
      "Sid": "Lambda"
    },
    {
      "Action": [
        "logs:UntagResource",
        "logs:UntagLogGroup",
        "logs:TagResource",
        "logs:TagLogGroup",
        "logs:PutRetentionPolicy",
        "logs:ListTagsLogGroup",
        "logs:ListTagsForResource",
        "logs:DisassociateKmsKey",
        "logs:DeleteRetentionPolicy",
        "logs:DeleteLogGroup",
        "logs:CreateLogGroup",
        "logs:AssociateKmsKey"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:logs:<REGIAO>:<CONTA>:log-group:/aws/lambda/<project>-*",
      "Sid": "Logs"
    },
    {
      "Action": "logs:DescribeLogGroups",
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "LogsList"
    },
    {
      "Action": [
        "s3:PutLifecycleConfiguration",
        "s3:PutEncryptionConfiguration",
        "s3:PutBucketVersioning",
        "s3:PutBucketTagging",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketPolicy",
        "s3:PutBucketOwnershipControls",
        "s3:PutBucketNotification",
        "s3:PutBucketLogging",
        "s3:ListBucketMultipartUploads",
        "s3:ListBucket",
        "s3:GetReplicationConfiguration",
        "s3:GetLifecycleConfiguration",
        "s3:GetEncryptionConfiguration",
        "s3:GetBucket*",
        "s3:GetAccelerateConfiguration",
        "s3:DeleteBucketPolicy",
        "s3:DeleteBucket",
        "s3:CreateBucket"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:s3:::<BUCKET_RAW>-access-logs",
        "arn:aws:s3:::<BUCKET_RAW>"
      ],
      "Sid": "RawAndLogsBuckets"
    },
    {
      "Action": [
        "s3:PutObject",
        "s3:ListMultipartUploadParts",
        "s3:GetObject",
        "s3:AbortMultipartUpload"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:s3:::<BUCKET_RAW>/*",
        "arn:aws:s3:::<BUCKET_RAW>-access-logs/*"
      ],
      "Sid": "RawAndLogsObjects"
    },
    {
      "Action": [
        "glue:UpdateTable",
        "glue:UpdateDatabase",
        "glue:UntagResource",
        "glue:TagResource",
        "glue:GetTags",
        "glue:GetTables",
        "glue:GetTableVersions",
        "glue:GetTable",
        "glue:GetPartitions",
        "glue:GetPartitionIndexes",
        "glue:GetPartition",
        "glue:GetDatabase",
        "glue:DeleteTable",
        "glue:DeleteDatabase",
        "glue:CreateTable",
        "glue:CreatePartition",
        "glue:CreateDatabase",
        "glue:BatchGetPartition",
        "glue:BatchCreatePartition"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:glue:<REGIAO>:<CONTA>:table/trusted/*",
        "arn:aws:glue:<REGIAO>:<CONTA>:table/raw/*",
        "arn:aws:glue:<REGIAO>:<CONTA>:database/trusted",
        "arn:aws:glue:<REGIAO>:<CONTA>:database/raw",
        "arn:aws:glue:<REGIAO>:<CONTA>:catalog"
      ],
      "Sid": "Glue"
    },
    {
      "Action": [
        "athena:StartQueryExecution",
        "athena:GetWorkGroup",
        "athena:GetQueryResults",
        "athena:GetQueryExecution"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:athena:<REGIAO>:<CONTA>:workgroup/primary",
      "Sid": "AthenaViewsAndRepair"
    },
    {
      "Action": [
        "events:UntagResource",
        "events:TagResource",
        "events:RemoveTargets",
        "events:PutTargets",
        "events:PutRule",
        "events:ListTargetsByRule",
        "events:ListTagsForResource",
        "events:DescribeRule",
        "events:DeleteRule"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:events:<REGIAO>:<CONTA>:rule/<project>-*",
      "Sid": "PartitionRegistrarRule"
    },
    {
      "Action": [
        "lambda:RemovePermission",
        "lambda:GetPolicy"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:lambda:<REGIAO>:<CONTA>:function:<project>-*",
      "Sid": "RegistrarPermissionRead"
    },
    {
      "Action": "lambda:AddPermission",
      "Condition": {
        "StringEquals": {
          "lambda:Principal": "events.amazonaws.com"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:lambda:<REGIAO>:<CONTA>:function:<project>-*",
      "Sid": "RegistrarInvokedOnlyByEventBridge"
    }
  ]
}
```

### Role da conta produtora: `gha-<project>-producer-<fonte>`

**Relação de confiança**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<CONTA>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:<dono>@<id-do-dono>/<repositorio>@<id-do-repositorio>:environment:producer-<fonte>"
        }
      }
    }
  ]
}
```

**Política de permissões** (em linha, nome `deploy`)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": [
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_STATE>",
      "Sid": "StateList"
    },
    {
      "Action": [
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_STATE>/producer/<fonte>.tfstate",
      "Sid": "StateObject"
    },
    {
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_STATE>/producer/<fonte>.tfstate.tflock",
      "Sid": "StateLock"
    },
    {
      "Action": "kms:CreateKey",
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/project": "<project>"
        }
      },
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "KmsCreateTagged"
    },
    {
      "Action": "kms:ListAliases",
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "KmsList"
    },
    {
      "Action": [
        "kms:UpdateAlias",
        "kms:DeleteAlias",
        "kms:CreateAlias"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:kms:<REGIAO>:<CONTA>:alias/<BUCKET_RAW>",
        "arn:aws:kms:<REGIAO>:<CONTA>:alias/<project>-*"
      ],
      "Sid": "KmsAliases"
    },
    {
      "Action": [
        "kms:UpdateKeyDescription",
        "kms:UpdateAlias",
        "kms:UntagResource",
        "kms:TagResource",
        "kms:ScheduleKeyDeletion",
        "kms:RetireGrant",
        "kms:ReEncryptTo",
        "kms:ReEncryptFrom",
        "kms:PutKeyPolicy",
        "kms:ListResourceTags",
        "kms:ListGrants",
        "kms:GetKeyRotationStatus",
        "kms:GetKeyPolicy",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:GenerateDataKey",
        "kms:Encrypt",
        "kms:EnableKeyRotation",
        "kms:EnableKey",
        "kms:DescribeKey",
        "kms:DeleteAlias",
        "kms:Decrypt",
        "kms:CreateGrant",
        "kms:CreateAlias"
      ],
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/project": "<project>"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:kms:*:*:key/*",
      "Sid": "KmsProjectKeys"
    },
    {
      "Action": [
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePolicy",
        "iam:CreateRole"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PermissionsBoundary": "arn:aws:iam::<CONTA>:policy/gha-<project>-boundary"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:iam::<CONTA>:role/<project>-*",
      "Sid": "ProjectRolesWithBoundary"
    },
    {
      "Action": [
        "iam:UpdateRole",
        "iam:UntagRole",
        "iam:TagRole",
        "iam:ListRolePolicies",
        "iam:ListInstanceProfilesForRole",
        "iam:ListAttachedRolePolicies",
        "iam:GetRolePolicy",
        "iam:GetRole",
        "iam:DeleteRole"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:iam::<CONTA>:role/<project>-*",
      "Sid": "ProjectRoles"
    },
    {
      "Action": "iam:PassRole",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "lambda.amazonaws.com"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:iam::<CONTA>:role/<project>-*",
      "Sid": "PassOnlyToLambda"
    },
    {
      "Action": [
        "lambda:UpdateFunctionConfiguration",
        "lambda:UpdateFunctionCode",
        "lambda:UntagResource",
        "lambda:TagResource",
        "lambda:PutFunctionConcurrency",
        "lambda:ListVersionsByFunction",
        "lambda:ListTags",
        "lambda:GetFunctionConfiguration",
        "lambda:GetFunctionCodeSigningConfig",
        "lambda:GetFunction",
        "lambda:DeleteFunctionConcurrency",
        "lambda:DeleteFunction",
        "lambda:CreateFunction"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:lambda:<REGIAO>:<CONTA>:function:<project>-*",
      "Sid": "Lambda"
    },
    {
      "Action": [
        "logs:UntagResource",
        "logs:UntagLogGroup",
        "logs:TagResource",
        "logs:TagLogGroup",
        "logs:PutRetentionPolicy",
        "logs:ListTagsLogGroup",
        "logs:ListTagsForResource",
        "logs:DisassociateKmsKey",
        "logs:DeleteRetentionPolicy",
        "logs:DeleteLogGroup",
        "logs:CreateLogGroup",
        "logs:AssociateKmsKey"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:logs:<REGIAO>:<CONTA>:log-group:/aws/lambda/<project>-*",
      "Sid": "Logs"
    },
    {
      "Action": "logs:DescribeLogGroups",
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "LogsList"
    },
    {
      "Action": [
        "dynamodb:UpdateTable",
        "dynamodb:UpdateItem",
        "dynamodb:UpdateContinuousBackups",
        "dynamodb:UntagResource",
        "dynamodb:TagResource",
        "dynamodb:PutItem",
        "dynamodb:ListTagsOfResource",
        "dynamodb:GetItem",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeContinuousBackups",
        "dynamodb:DeleteTable",
        "dynamodb:DeleteItem",
        "dynamodb:CreateTable"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:dynamodb:<REGIAO>:<CONTA>:table/<project>-*",
      "Sid": "ParameterTable"
    },
    {
      "Action": [
        "dynamodb:ListTagsOfResource",
        "dynamodb:ExportTableToPointInTime",
        "dynamodb:DescribeTimeToLive",
        "dynamodb:DescribeTable",
        "dynamodb:DescribeContinuousBackups"
      ],
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/project": "<project>"
        }
      },
      "Effect": "Allow",
      "Resource": "arn:aws:dynamodb:<REGIAO>:<CONTA>:table/*",
      "Sid": "TaggedSourceTablesReadAndExport"
    },
    {
      "Action": "dynamodb:DescribeExport",
      "Effect": "Allow",
      "Resource": "arn:aws:dynamodb:<REGIAO>:<CONTA>:table/*/export/*",
      "Sid": "ExportStatus"
    },
    {
      "Action": [
        "s3:PutObject",
        "s3:AbortMultipartUpload"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:s3:::<BUCKET_RAW>/*-initial/*",
      "Sid": "InitialExportToRaw"
    },
    {
      "Action": [
        "lambda:UpdateEventSourceMapping",
        "lambda:DeleteEventSourceMapping",
        "lambda:CreateEventSourceMapping"
      ],
      "Condition": {
        "ArnLike": {
          "lambda:FunctionArn": "arn:aws:lambda:<REGIAO>:<CONTA>:function:<project>-*"
        }
      },
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "EventSourceMappings"
    },
    {
      "Action": [
        "lambda:ListEventSourceMappings",
        "lambda:GetEventSourceMapping"
      ],
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "EventSourceMappingsRead"
    },
    {
      "Action": [
        "lambda:UntagResource",
        "lambda:TagResource",
        "lambda:ListTags"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:lambda:<REGIAO>:<CONTA>:event-source-mapping:*",
      "Sid": "EventSourceMappingsTags"
    },
    {
      "Action": [
        "sqs:UntagQueue",
        "sqs:TagQueue",
        "sqs:SetQueueAttributes",
        "sqs:ListQueueTags",
        "sqs:GetQueueUrl",
        "sqs:GetQueueAttributes",
        "sqs:DeleteQueue",
        "sqs:CreateQueue"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:sqs:<REGIAO>:<CONTA>:<project>-*",
      "Sid": "OnFailureQueue"
    },
    {
      "Action": [
        "sns:UntagResource",
        "sns:Unsubscribe",
        "sns:TagResource",
        "sns:Subscribe",
        "sns:SetTopicAttributes",
        "sns:ListTagsForResource",
        "sns:ListSubscriptionsByTopic",
        "sns:GetTopicAttributes",
        "sns:GetSubscriptionAttributes",
        "sns:DeleteTopic",
        "sns:CreateTopic"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:sns:<REGIAO>:<CONTA>:<project>-*",
      "Sid": "AlarmTopic"
    },
    {
      "Action": [
        "cloudwatch:UntagResource",
        "cloudwatch:TagResource",
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:PutDashboard",
        "cloudwatch:ListTagsForResource",
        "cloudwatch:GetDashboard",
        "cloudwatch:DeleteDashboards",
        "cloudwatch:DeleteAlarms"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:cloudwatch:<REGIAO>:<CONTA>:alarm:<project>-*",
        "arn:aws:cloudwatch::<CONTA>:dashboard/<project>-*"
      ],
      "Sid": "AlarmsAndDashboard"
    },
    {
      "Action": "cloudwatch:DescribeAlarms",
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "AlarmRead"
    }
  ]
}
```
