"""Loads and validates the forwarder's own config: which local FTP
account directories map to which loader push-mode source, and where
each account's API key actually lives on disk.

The API key is deliberately never inline in this file -- it lives in
its own, separately permission-restricted (chmod 600) file per
account, referenced here only by path. Mirrors this whole project's
own "secrets live in their own restricted file, never inline" rule
(see /etc/loader-data-gov-gr.env on the loader side itself) -- this
config file can be more loosely permissioned since it holds no secret
material directly, just directory mappings and pointers to the real
secret files.
"""

from __future__ import annotations

import dataclasses
import pathlib

import yaml


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AccountConfig:
    name: str
    watch_dir: pathlib.Path
    source_name: str
    api_key_file: pathlib.Path

    def api_key(self) -> str:
        return self.api_key_file.read_text(encoding="utf-8").strip()


@dataclasses.dataclass(frozen=True)
class ForwarderConfig:
    loader_base_url: str
    accounts: tuple[AccountConfig, ...]
    archive_subdir: str = ".forwarded"
    max_retries: int = 5
    retry_backoff_s: float = 1.0
    worker_count: int = 8
    catch_up_interval_s: int = 60


def _require(data: dict, key: str, context: str) -> object:
    if key not in data:
        raise ConfigError("%s: missing required key %r" % (context, key))
    return data[key]


def load_config(path: str | pathlib.Path) -> ForwarderConfig:
    raw_text = pathlib.Path(path).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML: %s" % exc) from exc
    return _build_config(data)


def _build_config(data: dict) -> ForwarderConfig:
    if not isinstance(data, dict):
        raise ConfigError("top-level config must be a mapping")

    loader_base_url = _require(data, "loader_base_url", "config")
    accounts_raw = _require(data, "accounts", "config")
    if not accounts_raw:
        raise ConfigError("config: 'accounts' must list at least one account")

    accounts = []
    seen_names = set()
    for entry in accounts_raw:
        name = _require(entry, "name", "account")
        if name in seen_names:
            raise ConfigError("account %r: duplicate name" % name)
        seen_names.add(name)
        accounts.append(AccountConfig(
            name=name,
            watch_dir=pathlib.Path(_require(entry, "watch_dir", "account %r" % name)),
            source_name=_require(entry, "source_name", "account %r" % name),
            api_key_file=pathlib.Path(_require(entry, "api_key_file", "account %r" % name)),
        ))

    return ForwarderConfig(
        loader_base_url=loader_base_url,
        accounts=tuple(accounts),
        archive_subdir=data.get("archive_subdir", ForwarderConfig.archive_subdir),
        max_retries=data.get("max_retries", ForwarderConfig.max_retries),
        retry_backoff_s=data.get("retry_backoff_s", ForwarderConfig.retry_backoff_s),
        worker_count=data.get("worker_count", ForwarderConfig.worker_count),
        catch_up_interval_s=data.get("catch_up_interval_s", ForwarderConfig.catch_up_interval_s),
    )
