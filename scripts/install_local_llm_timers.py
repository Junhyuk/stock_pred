#!/usr/bin/env python3
"""Install bounded local LLM user timers without changing production model rules."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_NAMES = (
    "roboquant-local-llm-close",
    "roboquant-local-llm-premarket",
    "roboquant-tomorrow-market-ml",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install RoboQuant local LLM user systemd timers.")
    parser.add_argument("--install", action="store_true", help="Write rendered units under ~/.config/systemd/user.")
    parser.add_argument("--enable", action="store_true", help="Enable and start timers after installation.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing the user service manager.")
    return parser.parse_args()


def render_unit(template: Path) -> str:
    return template.read_text(encoding="utf-8").replace("__PROJECT_ROOT__", str(ROOT)).replace("__PYTHON__", sys.executable)


def main() -> None:
    args = parse_args()
    if args.enable and not args.install:
        raise SystemExit("--enable requires --install so the rendered unit files are current.")

    target = Path.home() / ".config" / "systemd" / "user"
    templates = ROOT / "systemd"
    files = [templates / f"{name}.{suffix}" for name in UNIT_NAMES for suffix in ("service", "timer")]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise SystemExit(f"Missing timer templates: {', '.join(missing)}")

    if args.dry_run:
        for template in files:
            print(f"install {template.name} -> {target / template.name}")
        if args.enable:
            print("systemctl --user daemon-reload")
            for name in UNIT_NAMES:
                print(f"systemctl --user enable --now {name}.timer")
        return

    if args.install:
        target.mkdir(parents=True, exist_ok=True)
        for template in files:
            (target / template.name).write_text(render_unit(template), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    if args.enable:
        for name in UNIT_NAMES:
            subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"], check=True)


if __name__ == "__main__":
    main()
