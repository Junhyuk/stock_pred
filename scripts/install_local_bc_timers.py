#!/usr/bin/env python3
"""Render (and only on request install) the local Model B/C user timers.

Dry run is the default. Nothing is written unless --install is given, and the
timers start only with --install --enable. Run it with the forecast venv so
the rendered units use that interpreter:

  .venv-forecast/bin/python scripts/install_local_bc_timers.py               # print
  .venv-forecast/bin/python scripts/install_local_bc_timers.py --install --enable
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_NAMES = ("roboquant-bc-evening", "roboquant-bc-premarket")


def render_unit(template: Path, python: str) -> str:
    return template.read_text(encoding="utf-8").replace("__PROJECT_ROOT__", str(ROOT)).replace("__PYTHON__", python)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true", help="write the units under ~/.config/systemd/user")
    parser.add_argument("--enable", action="store_true", help="enable and start the timers (needs --install)")
    parser.add_argument("--only", choices=UNIT_NAMES, action="append", help="limit to one timer; repeatable")
    args = parser.parse_args(argv)
    if args.enable and not args.install:
        raise SystemExit("--enable requires --install so the rendered unit files are current")
    names = tuple(args.only or UNIT_NAMES)
    python = sys.executable
    target = Path.home() / ".config" / "systemd" / "user"
    files = [ROOT / "systemd" / f"{name}.{suffix}" for name in names for suffix in ("service", "timer")]
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise SystemExit(f"missing templates: {', '.join(missing)}")
    if not args.install:
        for template in files:
            print(f"# {target / template.name}")
            print(render_unit(template, python))
        print("# not installed. To install and start:")
        print(f"{python} {ROOT / 'scripts' / 'install_local_bc_timers.py'} --install --enable")
        print("systemctl --user list-timers 'roboquant-bc-*'")
        return 0
    try:
        import exchange_calendars  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"run the installer with the forecast venv interpreter: {exc}") from exc
    target.mkdir(parents=True, exist_ok=True)
    for template in files:
        (target / template.name).write_text(render_unit(template, python), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.enable:
        for name in names:
            subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
