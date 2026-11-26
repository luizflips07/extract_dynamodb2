import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).parents[1] / "infra" / "github_oidc" / "find_oidc_provider.py"
_spec = importlib.util.spec_from_file_location("find_oidc_provider", _path)
oidc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oidc)

OWNER = "meu-state/dono/repo"
PROJECT = "meu-projeto"


def provider(tags=None, clients=("sts.amazonaws.com",)):
    return {"ClientIDList": list(clients), "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}


def test_cria_quando_nao_existe():
    assert oidc.decide(None, OWNER, PROJECT) is True


def test_reaproveita_provedor_de_terceiros():
    assert oidc.decide(provider(), OWNER, PROJECT) is False


def test_reaproveita_provedor_de_outra_stack():
    tags = {"oidc-stack": "outro-state/dono/outro", "managed-by": "terraform", "layer": "ci", "project": PROJECT}
    assert oidc.decide(provider(tags), OWNER, PROJECT) is False


def test_mantem_o_que_a_propria_stack_criou():
    assert oidc.decide(provider({"oidc-stack": OWNER}), OWNER, PROJECT) is True


def test_mantem_provedor_criado_antes_da_tag():
    tags = {"managed-by": "terraform", "layer": "ci", "project": PROJECT}
    assert oidc.decide(provider(tags), OWNER, PROJECT) is True


def test_erro_se_provedor_de_terceiros_nao_aceita_sts():
    with pytest.raises(SystemExit):
        oidc.decide(provider(clients=("outro",)), OWNER, PROJECT)
