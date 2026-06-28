#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FORBIDDEN_STAGED_PREFIXES = (
    ".env",
    "data/processed/",
    "models/",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh data, export GitHub Pages docs/, commit, and push."
    )
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--today-config", default="configs/today_update.yaml")
    parser.add_argument("--top50-config", default="configs/top50_normal.yaml")
    parser.add_argument("--output", default="docs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--skip-push", action="store_true")
    parser.add_argument("--no-stop-web", action="store_true")
    parser.add_argument("--message", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    steps: list[list[str]] = []
    if not args.no_stop_web:
        steps.append(["bash", "-lc", "fuser -k 8000/tcp 8501/tcp >/dev/null 2>&1 || true"])
    if not args.skip_refresh:
        steps.append(
            [
                sys.executable,
                "scripts/run_today_market_update.py",
                "--config",
                args.today_config,
            ]
        )
        steps.append(
            [
                sys.executable,
                "scripts/generate_market_up_down.py",
                "--config",
                args.top50_config,
            ]
        )
    steps.append(
        [
            sys.executable,
            "scripts/export_github_pages_snapshot.py",
            "--config",
            args.config,
            "--today-config",
            args.today_config,
            "--output",
            args.output,
        ]
    )

    for command in steps:
        _run(command, dry_run=args.dry_run)

    if args.dry_run:
        print("[dry-run] git add docs/ && git commit && git push origin HEAD")
        return

    _git_add_docs(ROOT / args.output)
    message = args.message.strip() or f"Publish GitHub Pages snapshot {date.today().isoformat()}"
    committed = _git_commit(message)
    if args.skip_push:
        print("skip-push: export/commit finished")
        return
    if committed:
        _run(["git", "push", "origin", "HEAD"], dry_run=False)
    else:
        print("nothing to commit; skipping push")


def _run(command: list[str], *, dry_run: bool) -> None:
    printable = " ".join(command)
    print(f"$ {printable}")
    if dry_run:
        return
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout).strip()
        raise SystemExit(f"command failed ({result.returncode}): {printable}\n{stderr[-2000:]}")


def _git_add_docs(output_dir: Path) -> None:
    if not output_dir.exists():
        raise SystemExit(f"output directory missing: {output_dir}")
    subprocess.run(["git", "add", str(output_dir)], cwd=ROOT, check=True)
    _assert_safe_staged_paths()


def _assert_safe_staged_paths() -> None:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    staged = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for path in staged:
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in FORBIDDEN_STAGED_PREFIXES):
            subprocess.run(["git", "reset", "HEAD", "--", path], cwd=ROOT, check=False)
            raise SystemExit(f"refusing to commit forbidden path: {path}")
        if not path.startswith("docs/"):
            subprocess.run(["git", "reset", "HEAD"], cwd=ROOT, check=False)
            raise SystemExit(f"refusing to commit non-docs path: {path}")


def _git_commit(message: str) -> bool:
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=ROOT,
        check=False,
    )
    if status.returncode == 0:
        print("nothing to commit")
        return False
    subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True)
    return True


if __name__ == "__main__":
    main()
