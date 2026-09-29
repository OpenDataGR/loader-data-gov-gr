from ftp_forwarder.config import ForwarderConfig


def test_applies_defaults_for_optional_fields():
    config = ForwarderConfig(loader_base_url="https://loader.data.gov.gr", sync_key="the-key")
    assert config.archive_subdir == ".forwarded"
    assert config.max_retries == 5
    assert config.retry_backoff_s == 1.0
    assert config.worker_count == 8
    assert config.catch_up_interval_s == 60


def test_overrides_are_respected():
    config = ForwarderConfig(
        loader_base_url="https://loader.data.gov.gr",
        sync_key="the-key",
        archive_subdir=".done",
        max_retries=3,
        retry_backoff_s=2.5,
        worker_count=4,
        catch_up_interval_s=30,
    )
    assert config.archive_subdir == ".done"
    assert config.max_retries == 3
    assert config.retry_backoff_s == 2.5
    assert config.worker_count == 4
    assert config.catch_up_interval_s == 30
