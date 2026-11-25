# Cadastra no GitHub o que o deploy.yml precisa: environments, variaveis e secrets.
# Pre-requisitos: GitHub CLI autenticada (gh auth login) e a stack infra/github_oidc aplicada.
# -ConfigRoot: pasta com config.yaml e sources/ com os valores REAIS (nada e gravado no repositorio).
param(
    [Parameter(Mandatory)] [string] $Repo,                 # dono/repositorio
    [Parameter(Mandatory)] [string] $ConfigRoot,
    [Parameter(Mandatory)] [string] $StateBucket,
    [Parameter(Mandatory)] [string] $DatalakeRoleArn,      # output datalake_role_arn
    [Parameter(Mandatory)] [hashtable] $ProducerRoleArns   # output producer_role_arns, ex.: @{ local = "arn:..." }
)
$ErrorActionPreference = "Stop"

function Invoke-Gh {
    & gh @args
    if ($LASTEXITCODE -ne 0) { throw "falhou: gh $($args[0]) $($args[1])" }
}

$reader = "import json,sys,yaml,pathlib; r=pathlib.Path(sys.argv[1]); " +
          "c=yaml.safe_load((r/'config.yaml').read_text(encoding='utf-8')); " +
          "s={p.stem: yaml.safe_load(p.read_text(encoding='utf-8'))['account_id'] for p in (r/'sources').glob('*.yaml')}; " +
          "print(json.dumps({'region': c['region'], 'datalake': c['datalake'], 'emails': c['alarms']['notification_emails'], 'sources': s}))"
$cfg = (& python -c $reader $ConfigRoot) | ConvertFrom-Json
if (-not $cfg) { throw "nao foi possivel ler $ConfigRoot" }
if ($cfg.datalake.account_id -eq "111111111111") { throw "$ConfigRoot tem valores de exemplo" }

Invoke-Gh auth status

# ---------- Environments: so a main faz deploy; datalake-prod pede aprovacao ----------
$me = (& gh api user | ConvertFrom-Json).id
$environments = @("datalake-prod") + @($ProducerRoleArns.Keys | ForEach-Object { "producer-$_" })
foreach ($name in $environments) {
    $body = @{ deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true } }
    if ($name -eq "datalake-prod") { $body.reviewers = @(@{ type = "User"; id = $me }) }
    $body | ConvertTo-Json -Depth 5 -Compress | & gh api --method PUT "repos/$Repo/environments/$name" --input - | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "falhou ao criar o environment $name" }
    $policies = & gh api "repos/$Repo/environments/$name/deployment-branch-policies" | ConvertFrom-Json
    if ($policies.branch_policies.name -notcontains "main") {
        Invoke-Gh api --method POST "repos/$Repo/environments/$name/deployment-branch-policies" -f name=main | Out-Null
    }
    Write-Host "environment $name ok"
}

# ---------- Variaveis (nao sensiveis) ----------
Invoke-Gh variable set AWS_REGION --repo $Repo --body $cfg.region
Invoke-Gh variable set TFSTATE_BUCKET --repo $Repo --body $StateBucket

# ---------- Secrets (mascarados nos logs, que sao publicos em repositorio publico) ----------
$accounts = ($cfg.sources.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ","
$secrets = @{
    DATALAKE_ACCOUNT_ID      = $cfg.datalake.account_id
    RAW_BUCKET               = $cfg.datalake.raw_bucket
    ALARM_EMAILS             = ($cfg.emails -join ",")
    SOURCE_ACCOUNT_IDS       = $accounts
    DATALAKE_DEPLOY_ROLE_ARN = $DatalakeRoleArn
}
foreach ($key in $secrets.Keys) {
    Invoke-Gh secret set $key --repo $Repo --body $secrets[$key]
}
foreach ($source in $ProducerRoleArns.Keys) {
    Invoke-Gh secret set PRODUCER_DEPLOY_ROLE_ARN --repo $Repo --env "producer-$source" --body $ProducerRoleArns[$source]
}
Write-Host "GitHub configurado. Dispare o deploy em Actions > deploy > Run workflow."
