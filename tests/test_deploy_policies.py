"""Travas estáticas de infra/github_oidc: o que não pode voltar a ficar aberto."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
OIDC = (ROOT / "infra" / "github_oidc" / "main.tf").read_text(encoding="utf-8")


def statement(sid: str) -> str:
    return re.search(rf'sid\s*=\s*"{sid}".*?(?=\n  statement \{{|\n\}}\n)', OIDC, re.S).group(0)


def test_sem_curinga_de_servico_nas_acoes():
    assert not re.findall(r'"[a-z0-9-]+:\*"', OIDC)


def test_confianca_so_por_environment():
    assert "ref:refs/heads" not in OIDC
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    jobs = re.split(r"\n  (?=[a-z_]+:\n)", workflow.split("\njobs:")[1])
    for job in jobs:
        if "configure-aws-credentials" in job:
            assert re.search(r"\n    environment: ", job), job.splitlines()[0]


def test_mudar_o_que_uma_role_pode_exige_o_boundary():
    for action in ("CreateRole", "PutRolePolicy", "UpdateAssumeRolePolicy", "PutRolePermissionsBoundary"):
        assert OIDC.count(f'"iam:{action}"') == 1
        assert f'"iam:{action}"' in statement("ProjectRolesWithBoundary")
    assert "iam:PermissionsBoundary" in statement("ProjectRolesWithBoundary")
    assert "iam:DeleteRolePermissionsBoundary" not in OIDC


def test_roles_das_lambdas_usam_o_boundary():
    for path in ("infra/producer/main.tf", "infra/datalake/partition_registrar.tf"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert text.count('resource "aws_iam_role" ') == text.count("permissions_boundary = ")


def test_kms_nao_adota_chave_de_outro_projeto():
    assert OIDC.count('"kms:TagResource"') == 1
    assert "aws:ResourceTag/project" in statement("KmsProjectKeys")
    assert "aws:RequestTag/project" in statement("KmsCreateTagged")


def test_tabelas_de_origem_so_com_a_tag_do_projeto():
    assert "aws:ResourceTag/project" in statement("TaggedSourceTablesReadAndExport")
