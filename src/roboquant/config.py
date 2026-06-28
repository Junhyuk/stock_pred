from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML config and normalize project-relative paths."""
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    root = config_path.parents[1] if config_path.parent.name == "configs" else Path.cwd()
    config["_root"] = str(root)

    paths = config.setdefault("paths", {})
    for key, value in list(paths.items()):
        if value is None:
            continue
        value_path = Path(value)
        if not value_path.is_absolute():
            paths[key] = str((root / value_path).resolve())

    return config


def ensure_project_dirs(config: dict[str, Any]) -> None:
    for key in ("raw_dir", "interim_dir", "processed_dir", "model_dir", "report_dir"):
        path = config.get("paths", {}).get(key)
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)


def get_horizons(config: dict[str, Any]) -> dict[str, int]:
    horizons = config.get("horizons", {})
    return {str(name): int(days) for name, days in horizons.items()}


def get_feature_columns(config: dict[str, Any]) -> list[str]:
    return list(config.get("features", {}).get("columns", []))


def get_database_path(config: dict[str, Any]) -> Path:
    database = config.get("paths", {}).get("database")
    if not database:
        raise ValueError("Config is missing paths.database")
    return Path(database)

