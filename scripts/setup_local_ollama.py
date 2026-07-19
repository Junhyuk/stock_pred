#!/usr/bin/env python3
"""Check and optionally import the local-only MarketNewsFeed GGUF into Ollama."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import load_config
from roboquant.signals.marketnewsfeed_llm import (
    LocalOllamaClient,
    OllamaUnavailableError,
    settings_from_config,
)


@dataclass(frozen=True)
class OllamaSetupSettings:
    ollama_host: str
    model_name: str
    gguf_path: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check or import the local Ollama news model.")
    parser.add_argument("--config", default="configs/local_llm_news.yaml")
    parser.add_argument("--import-model", action="store_true")
    parser.add_argument("--install-user-service", action="store_true")
    parser.add_argument("--start-service", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = setup_local_ollama(
        config_path=args.config,
        import_model=args.import_model,
        install_user_service=args.install_user_service,
        start_service=args.start_service,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False))


def setup_local_ollama(
    *,
    config_path: str | Path = "configs/local_llm_news.yaml",
    import_model: bool = False,
    install_user_service: bool = False,
    start_service: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    config = load_config(config_path)
    settings = _setup_settings(config)
    binary = shutil.which("ollama")
    gguf_path = _resolve_project_path(config, settings.gguf_path)
    result: dict[str, object] = {
        "status": "ready",
        "ollama_binary": binary,
        "host": settings.ollama_host,
        "model_name": settings.model_name,
        "gguf_path": str(gguf_path),
        "gguf_exists": gguf_path.is_file(),
        "disk_free_bytes": shutil.disk_usage(gguf_path.parent if gguf_path.parent.exists() else Path.home()).free,
        "gpu": _gpu_status(),
        "server": "unavailable",
        "model": "not_checked",
        "service": _user_service_status(),
    }
    if binary is None:
        result.update(
            {
                "status": "ollama_not_installed",
                "hint": "Install Ollama with the official Linux instructions, then rerun this command.",
            }
        )
        return result
    if not gguf_path.is_file():
        result.update({"status": "gguf_missing", "hint": "Place the selected GGUF under llm_models/gguf/."})
        return result

    if install_user_service:
        result["service"] = _install_user_service(binary, dry_run=dry_run)
    if start_service:
        result["service_start"] = _start_user_service(dry_run=dry_run)
    if import_model:
        required_bytes = max(gguf_path.stat().st_size * 2, 24 * 1024**3)
        free_bytes = int(result["disk_free_bytes"])
        if free_bytes < required_bytes:
            result.update(
                {
                    "status": "model_import_failed",
                    "model_import": {
                        "ok": False,
                        "status": "insufficient_disk_space",
                        "required_free_bytes": required_bytes,
                        "available_free_bytes": free_bytes,
                    },
                }
            )
            return result
        result["model_import"] = _import_model(binary, settings.model_name, gguf_path, dry_run=dry_run)
        if not bool(result["model_import"].get("ok")):
            result["status"] = "model_import_failed"
            return result

    try:
        capability = LocalOllamaClient(settings.ollama_host, timeout_seconds=10).probe(settings.model_name)
    except (OllamaUnavailableError, ValueError) as exc:
        message = str(exc)
        if message.startswith("model_import_failed:"):
            result.update(
                {
                    "status": "model_import_failed",
                    "server": "ready",
                    "model": "not_imported",
                    "model_error": message,
                }
            )
            return result
        result["server"] = "unavailable"
        result["server_error"] = message
        if import_model:
            result["status"] = "model_imported_server_unavailable"
        return result
    result["server"] = "ready"
    result["model"] = "ready"
    result["capability"] = capability
    return result


def _resolve_project_path(config: dict, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return Path(config["_root"]) / path


def _setup_settings(config: dict) -> OllamaSetupSettings | object:
    """Support both legacy MarketNewsFeed and integrated local-news configs."""
    local = config.get("local_llm_news")
    if isinstance(local, dict):
        return OllamaSetupSettings(
            ollama_host=str(local.get("ollama_host") or "http://127.0.0.1:11434"),
            model_name=str(local.get("model_name") or "roboquant-news:qwen3.5-27b-q4"),
            gguf_path=str(local.get("gguf_path") or "llm_models/gguf/Qwen3.5-27B-Q4_K_M.gguf"),
        )
    return settings_from_config(config)


def _import_model(binary: str, model_name: str, gguf_path: Path, *, dry_run: bool) -> dict[str, object]:
    modelfile = "\n".join(
        [
            f"FROM {gguf_path.resolve()}",
            "PARAMETER temperature 0",
            "PARAMETER num_ctx 4096",
            "SYSTEM You return concise structured research output. Do not provide investment instructions.",
            "",
        ]
    )
    command = [binary, "create", model_name, "-f"]
    if dry_run:
        return {"ok": True, "status": "dry_run", "command": [*command, "<temporary Modelfile>"]}
    with tempfile.NamedTemporaryFile("w", suffix=".Modelfile", encoding="utf-8", delete=False) as file:
        file.write(modelfile)
        path = Path(file.name)
    try:
        completed = subprocess.run(
            [*command, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "status": "failed", "error": str(exc)}
    finally:
        path.unlink(missing_ok=True)
    if completed.returncode != 0:
        return {"ok": False, "status": "failed", "error": _short_error(completed.stderr or completed.stdout)}
    return {"ok": True, "status": "imported"}


def _install_user_service(binary: str, *, dry_run: bool) -> dict[str, object]:
    target = Path.home() / ".config" / "systemd" / "user" / "roboquant-ollama.service"
    source = ROOT / "systemd" / "roboquant-ollama.service"
    if dry_run:
        return {"ok": True, "status": "dry_run", "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    content = source.read_text(encoding="utf-8").replace("/usr/bin/env ollama", str(Path(binary).resolve()))
    target.write_text(content, encoding="utf-8")
    completed = subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        return {"ok": False, "status": "installed_daemon_reload_failed", "path": str(target)}
    return {"ok": True, "status": "installed", "path": str(target)}


def _start_user_service(*, dry_run: bool) -> dict[str, object]:
    if dry_run:
        return {"ok": True, "status": "dry_run"}
    completed = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "roboquant-ollama.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {"ok": completed.returncode == 0, "status": "started" if completed.returncode == 0 else "failed"}


def _user_service_status() -> str:
    completed = subprocess.run(
        ["systemctl", "--user", "is-active", "roboquant-ollama.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip() == "active":
        return "active"
    return "not_installed" if "not-found" in (completed.stderr or "") else "inactive"


def _gpu_status() -> str:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError:
        return "not_detected"
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else "not_detected"


def _short_error(value: str) -> str:
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return " ".join(clean.split())[-500:]


if __name__ == "__main__":
    main()
