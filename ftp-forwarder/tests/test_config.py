import pathlib

import pytest

from ftp_forwarder.config import ConfigError, load_config


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "config.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_minimal_valid_config(tmp_path):
    path = _write(tmp_path, """
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: /etc/ftp-forwarder/secrets/acme.key
""")
    config = load_config(path)
    assert config.loader_base_url == "https://loader.data.gov.gr"
    assert len(config.accounts) == 1
    account = config.accounts[0]
    assert account.name == "acme"
    assert account.watch_dir == pathlib.Path("/srv/ftp/acme")
    assert account.source_name == "acme-push"


def test_applies_defaults_for_optional_fields(tmp_path):
    path = _write(tmp_path, """
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: /etc/ftp-forwarder/secrets/acme.key
""")
    config = load_config(path)
    assert config.archive_subdir == ".forwarded"
    assert config.max_retries == 5
    assert config.worker_count == 8
    assert config.catch_up_interval_s == 60


def test_overrides_are_respected(tmp_path):
    path = _write(tmp_path, """
loader_base_url: https://loader.data.gov.gr
archive_subdir: .done
max_retries: 3
retry_backoff_s: 2.5
worker_count: 4
catch_up_interval_s: 30
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: /etc/ftp-forwarder/secrets/acme.key
""")
    config = load_config(path)
    assert config.archive_subdir == ".done"
    assert config.max_retries == 3
    assert config.retry_backoff_s == 2.5
    assert config.worker_count == 4
    assert config.catch_up_interval_s == 30


def test_multiple_accounts(tmp_path):
    path = _write(tmp_path, """
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: /etc/ftp-forwarder/secrets/acme.key
  - name: other
    watch_dir: /srv/ftp/other
    source_name: other-push
    api_key_file: /etc/ftp-forwarder/secrets/other.key
""")
    config = load_config(path)
    assert [a.name for a in config.accounts] == ["acme", "other"]


def test_rejects_missing_loader_base_url(tmp_path):
    path = _write(tmp_path, """
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: /etc/ftp-forwarder/secrets/acme.key
""")
    with pytest.raises(ConfigError, match="loader_base_url"):
        load_config(path)


def test_rejects_empty_accounts_list(tmp_path):
    path = _write(tmp_path, "loader_base_url: https://loader.data.gov.gr\naccounts: []\n")
    with pytest.raises(ConfigError, match="accounts"):
        load_config(path)


def test_rejects_an_account_missing_a_required_field(tmp_path):
    path = _write(tmp_path, """
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
""")
    with pytest.raises(ConfigError, match="source_name"):
        load_config(path)


def test_rejects_duplicate_account_names(tmp_path):
    path = _write(tmp_path, """
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: /etc/ftp-forwarder/secrets/acme.key
  - name: acme
    watch_dir: /srv/ftp/acme2
    source_name: acme-push-2
    api_key_file: /etc/ftp-forwarder/secrets/acme2.key
""")
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path)


def test_rejects_invalid_yaml(tmp_path):
    path = _write(tmp_path, "loader_base_url: [unterminated\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_account_api_key_reads_from_its_own_file(tmp_path):
    key_file = tmp_path / "acme.key"
    key_file.write_text("  secret-key-value  \n", encoding="utf-8")
    path = _write(tmp_path, f"""
loader_base_url: https://loader.data.gov.gr
accounts:
  - name: acme
    watch_dir: /srv/ftp/acme
    source_name: acme-push
    api_key_file: {key_file}
""")
    config = load_config(path)
    assert config.accounts[0].api_key() == "secret-key-value"
