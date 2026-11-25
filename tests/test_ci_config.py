import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_config  # noqa: E402

ENV = {
    "DATALAKE_ACCOUNT_ID": "123456789012",
    "RAW_BUCKET": "bucket-real",
    "RAW_KMS_KEY_ID": "abc-123",
    "ALARM_EMAILS": "a@dominio.com, b@dominio.com",
    "SOURCE_ACCOUNT_IDS": "local=210987654321",
}


def _copy(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "sources", tmp_path / "sources")
    return tmp_path


def _mkdir(path: Path) -> Path:
    path.mkdir()
    return path


def test_replaces_every_placeholder_and_keeps_the_rest(tmp_path):
    root = _copy(tmp_path)
    assert ci_config.main(root, ENV) == 0
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    original = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["datalake"]["account_id"] == "123456789012"
    assert cfg["datalake"]["raw_bucket"] == "bucket-real"
    assert cfg["datalake"]["raw_kms_key_arn"] == "arn:aws:kms:us-east-1:123456789012:key/abc-123"
    assert cfg["alarms"]["notification_emails"] == ["a@dominio.com", "b@dominio.com"]
    assert cfg["datalake"]["partition_start_date"] == original["datalake"]["partition_start_date"]
    assert (cfg["project"], cfg["streaming"]) == (original["project"], original["streaming"])
    assert yaml.safe_load((root / "sources" / "local.yaml").read_text(encoding="utf-8"))["account_id"] == "210987654321"


def test_fails_when_a_secret_is_missing(tmp_path):
    env = {k: v for k, v in ENV.items() if k != "RAW_BUCKET"}
    assert ci_config.main(_copy(tmp_path), env) == 1


def test_data_lake_job_may_run_before_the_key_exists(tmp_path):
    env = {k: v for k, v in ENV.items() if k != "RAW_KMS_KEY_ID"}
    assert ci_config.main(_copy(tmp_path), env) == 1
    assert ci_config.main(_copy(_mkdir(tmp_path / "b")), env | {"KMS_OPTIONAL": "1"}) == 0


def test_rejects_values_that_would_break_the_yaml(tmp_path):
    assert ci_config.main(_copy(tmp_path), ENV | {"RAW_BUCKET": 'x"\nproject: outro'}) == 1
    assert ci_config.main(_copy(_mkdir(tmp_path / "b")), ENV | {"SOURCE_ACCOUNT_IDS": "local=abc"}) == 1
