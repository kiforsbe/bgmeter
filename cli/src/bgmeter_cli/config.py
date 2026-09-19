"""Versioned persistent driver registration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from platformdirs import user_config_path


CONFIG_SCHEMA = "bgmeter.driver-registration"
CONFIG_VERSION = 1
DEFAULT_REGISTERED_DRIVERS = ("microtech",)


class ConfigError(ValueError):
    """The persistent CLI configuration is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class DriverConfig:
    registered: tuple[str, ...] = DEFAULT_REGISTERED_DRIVERS

    def __post_init__(self) -> None:
        registered = tuple(self.registered)
        if any(not isinstance(item, str) or not item for item in registered):
            raise ConfigError("registered drivers must be non-empty strings")
        if len(set(registered)) != len(registered):
            raise ConfigError("registered drivers must be unique")
        object.__setattr__(self, "registered", registered)


def default_config_path() -> Path:
    return Path(user_config_path("bgmeter")) / "drivers.json"


def load_config(path: str | Path | None = None) -> DriverConfig:
    target = Path(path) if path is not None else default_config_path()
    if not target.exists():
        return DriverConfig()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read driver configuration: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError("driver configuration must be a JSON object")
    if document.get("schema") != CONFIG_SCHEMA:
        raise ConfigError("unsupported driver configuration schema")
    if document.get("schema_version") != CONFIG_VERSION:
        raise ConfigError(
            f"unsupported driver configuration version {document.get('schema_version')!r}"
        )
    registered = document.get("registered")
    if not isinstance(registered, list):
        raise ConfigError("driver configuration registered field must be a list")
    return DriverConfig(tuple(registered))


def save_config(config: DriverConfig, path: str | Path | None = None) -> None:
    target = Path(path) if path is not None else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema": CONFIG_SCHEMA,
        "schema_version": CONFIG_VERSION,
        "registered": list(config.registered),
    }
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


__all__ = [
    "CONFIG_SCHEMA",
    "CONFIG_VERSION",
    "ConfigError",
    "DriverConfig",
    "default_config_path",
    "load_config",
    "save_config",
]
