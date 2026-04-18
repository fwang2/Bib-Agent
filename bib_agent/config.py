from __future__ import annotations

import json
from pathlib import Path


def load_config(path: str | Path) -> dict:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    data["_config_path"] = str(config_path.resolve())
    data["_root_dir"] = str(config_path.resolve().parent)
    return data


def save_config(config: dict, path: str | Path | None = None) -> None:
    config_path = Path(path or config.get("_config_path") or "config.json")
    payload = {key: value for key, value in config.items() if not key.startswith("_")}
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def resolve_path(config: dict, value: str) -> Path:
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return Path(config["_root_dir"], expanded).resolve()


def active_bib_files(config: dict) -> dict[str, dict]:
    return {
        name: bib_config
        for name, bib_config in config.get("bib_files", {}).items()
        if bib_config.get("enabled", True)
    }


def resolve_routed_category(config: dict, publication_type: str) -> str:
    active = active_bib_files(config)
    if not active:
        raise ValueError("No enabled bib_files are configured.")

    routing = config.get("routing", {})
    target = routing.get(publication_type) or routing.get("default")
    if target in active:
        return target

    if publication_type in active:
        return publication_type

    return next(iter(active))
