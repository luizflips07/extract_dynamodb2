import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clone_request as cr  # noqa: E402

GOOD = {
    cr.NAME_FIELD: "novo-repo",
    cr.PROJECT_FIELD: "dynamodb-vendas",
    cr.REGION_FIELD: "sa-east-1",
    cr.BUCKET_FIELD: "empresa-vendas-raw-001",
    cr.ACCOUNT_FIELD: "123456789012",
}
FORM = ROOT / ".github" / "ISSUE_TEMPLATE" / "clonar-repositorio.yml"


def _body(**changes: str) -> str:
    keys = {"name": cr.NAME_FIELD, "project": cr.PROJECT_FIELD, "region": cr.REGION_FIELD,
            "bucket": cr.BUCKET_FIELD, "account": cr.ACCOUNT_FIELD}
    values = GOOD | {keys[k]: v for k, v in changes.items()}
    return "".join(f"### {label}\r\n\r\n{value}\r\n\r\n" for label, value in values.items())


def test_reads_all_required_fields():
    assert cr.parse(_body()) == GOOD


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": ".."}, {"name": "outro/dono"}, {"name": "x;rm -rf ."},
    {"project": ""}, {"project": "Vendas"}, {"project": "ab"}, {"project": "a" * 31},
    {"region": ""}, {"region": "brasil"}, {"region": "us-east-1; id"},
    {"bucket": ""}, {"bucket": "Bucket_X"}, {"bucket": "a" * 52},
    {"account": ""}, {"account": "12345"}, {"account": "_No response_"},
])
def test_rejects_missing_or_invalid_values(changes):
    with pytest.raises(ValueError):
        cr.parse(_body(**changes))


def test_reports_every_problem_at_once():
    with pytest.raises(ValueError) as error:
        cr.parse(_body(region="", account="x"))
    assert cr.REGION_FIELD in str(error.value) and cr.ACCOUNT_FIELD in str(error.value)


def test_rejects_free_text_issue():
    with pytest.raises(ValueError):
        cr.parse("quero um clone")


def _repo(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "sources", tmp_path / "sources")
    return tmp_path


def test_main_writes_the_values_into_the_copy(tmp_path, monkeypatch):
    root, output = _repo(tmp_path), tmp_path / "out"
    monkeypatch.setenv("ISSUE_BODY", _body())
    monkeypatch.setenv("ROOT_PATH", str(root))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cr.main() == 0
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    assert (cfg["project"], cfg["tfstate_prefix"], cfg["region"]) == ("dynamodb-vendas", "dynamodb-vendas", "sa-east-1")
    assert cfg["datalake"]["account_id"] == "123456789012"
    assert cfg["datalake"]["raw_bucket"] == "empresa-vendas-raw-001"
    assert cfg["datalake"]["raw_kms_key_arn"].startswith("arn:aws:kms:sa-east-1:123456789012:key/0000")
    assert cfg["streaming"] == yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["streaming"]
    assert yaml.safe_load((root / "sources" / "local.yaml").read_text(encoding="utf-8"))["account_id"] == "123456789012"
    assert output.read_text(encoding="utf-8").splitlines() == ["repo_name=novo-repo", "project=dynamodb-vendas"]


def test_invalid_request_changes_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("ISSUE_BODY", _body(project="dynamodb-produtor"))  # igual ao de origem
    monkeypatch.setenv("ROOT_PATH", str(root))
    before = (root / "config.yaml").read_bytes()
    assert cr.main() == 1
    assert (root / "config.yaml").read_bytes() == before


def test_form_asks_every_field_as_required():
    form = yaml.safe_load(FORM.read_text(encoding="utf-8"))
    fields = {f["attributes"]["label"]: f["validations"]["required"] for f in form["body"] if f["type"] != "markdown"}
    assert fields == dict.fromkeys(cr.FIELDS, True)
    assert form["title"].startswith("[clone]")
