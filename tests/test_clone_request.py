import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clone_request as cr  # noqa: E402


def _body(name: str = "novo-repo", project: str = "dynamodb-vendas") -> str:
    return f"### {cr.NAME_FIELD}\r\n\r\n{name}\r\n\r\n### {cr.PROJECT_FIELD}\r\n\r\n{project}\r\n"


def test_reads_both_fields_from_issue_form():
    assert cr.repo_name(_body("extract-dynamodb_v2.1")) == "extract-dynamodb_v2.1"
    assert cr.project_name(_body()) == "dynamodb-vendas"


@pytest.mark.parametrize("name", ["", "..", "a b", "x;rm -rf .", "outro/dono", "$(id)", "a" * 101, "_No response_"])
def test_rejects_unsafe_or_empty_names(name):
    with pytest.raises(ValueError):
        cr.repo_name(_body(name=name))


@pytest.mark.parametrize("project", ["", "ab", "Vendas", "1vendas", "vendas_x", "vendas-", "a/b", "a" * 31])
def test_rejects_invalid_project(project):
    with pytest.raises(ValueError):
        cr.project_name(_body(project=project))


def test_rejects_free_text_issue():
    with pytest.raises(ValueError):
        cr.repo_name("quero um clone")


def test_sets_project_and_state_folder_keeping_the_rest_of_config():
    original = (ROOT / "config.yaml").read_bytes().decode("utf-8")
    updated = cr.apply_project(original, "dynamodb-vendas")
    before, after = yaml.safe_load(original), yaml.safe_load(updated)
    assert (after["project"], after["tfstate_prefix"]) == ("dynamodb-vendas", "dynamodb-vendas")
    assert {k: v for k, v in after.items() if k not in ("project", "tfstate_prefix")} == {
        k: v for k, v in before.items() if k not in ("project", "tfstate_prefix")
    }
    assert updated.count("#") == original.count("#")
    assert updated.count("\r\n") == original.count("\r\n")


def test_rejects_the_same_project_as_the_origin():
    with pytest.raises(ValueError):
        cr.apply_project('project: abc\ntfstate_prefix: ""\n', "abc")


def test_main_updates_config_and_writes_outputs(tmp_path, monkeypatch):
    config, output = tmp_path / "config.yaml", tmp_path / "out"
    config.write_text('project: origem\ntfstate_prefix: ""\n', encoding="utf-8")
    monkeypatch.setenv("ISSUE_BODY", _body())
    monkeypatch.setenv("CONFIG_PATH", str(config))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cr.main() == 0
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["tfstate_prefix"] == "dynamodb-vendas"
    assert output.read_text(encoding="utf-8").splitlines() == ["repo_name=novo-repo", "project=dynamodb-vendas"]


def test_form_labels_match_parser():
    form = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "clonar-repositorio.yml").read_text(encoding="utf-8"))
    labels = [f["attributes"].get("label") for f in form["body"]]
    assert cr.NAME_FIELD in labels and cr.PROJECT_FIELD in labels
    assert form["title"].startswith("[clone]")
