import logging
from pathlib import Path
from typing import Any

import toml

logger = logging.getLogger(__name__)

CONFIG_FILE = Path("data/config.toml")

DEFAULT_CONFIG = {
    "jutsu": {
        "login": "",
        "password": "",
    },
    "cooldown": {
        "min_ms": 2000,
        "max_ms": 5000,
    },
    "download": {
        "path": "data/downloads/",
        "quality": "720p",
        "quality_fallback": "lower",
        "parallel": 1,
        "min_size_mb": 1.0,
        "retry_attempts": 3,
        "retry_backoff_s": 5,
        "retry_max_backoff_s": 300,
        "parse_attempts": 3,
        "stub_trigger": 3,
        "cooldown_s": 900,
    },
    "telegram": {
        "api_id": 0,
        "api_hash": "",
        "target_dialog": "",
        "split_mb": 1900,
        "auto_send_enabled": False,
        "sleep_threshold": 60,
        "upload_retries": 3,
        "upload_offline_wait_s": 90,
        "upload_offline_deferrals": 5,
        "upload_stall_s": 90,
        "upload_probe_s": 60,
        "upload_min_total_mb": 5.0,
        "upload_flood_backoff_s": 600,
        "proxy": {
            "enabled": False,
            "scheme": "socks5",
            "host": "",
            "port": 9050,
            "username": "",
            "password": "",
        },
    },
}


def _ensure_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        return load()

    logger.info("Создание %s (настройки по умолчанию)", CONFIG_FILE)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    save(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def load() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)

    def _coerce(value: Any, default: Any) -> Any:
        if isinstance(default, int) and not isinstance(value, int):
            try:
                return int(value)
            except (ValueError, TypeError):
                return value
        if isinstance(default, float) and not isinstance(value, (int, float)):
            try:
                return float(value)
            except (ValueError, TypeError):
                return value
        return value

    def _merge(loaded: dict, defaults: dict) -> dict:
        result = dict(loaded)
        for k, default_val in defaults.items():
            if k not in result:
                result[k] = default_val
            elif isinstance(default_val, dict) and isinstance(result[k], dict):
                result[k] = _merge(result[k], default_val)
            else:
                result[k] = _coerce(result[k], default_val)
        return result

    with open(CONFIG_FILE, encoding="utf-8") as f:
        raw = dict(toml.load(f))

    return _merge(raw, DEFAULT_CONFIG)


def save(cfg: dict[str, Any]) -> None:
    """Сохраняет конфиг в TOML-файл."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        toml.dump(cfg, f)


def get(key: str, default: Any = None) -> Any:
    cfg = _ensure_config()
    parts = key.split(".")
    val: Any = cfg
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part, {})
        else:
            return default
    return val if val != {} else default


def set_value(key: str, value: Any) -> None:
    cfg = _ensure_config()
    parts = key.split(".")
    target = cfg
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    save(cfg)
