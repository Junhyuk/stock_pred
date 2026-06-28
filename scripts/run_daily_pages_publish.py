#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from export_github_pages_site import export_site

DEFAULT_SITE_DIR = ROOT / "reports" / "github_pages_site"
DEFAULT_WORKTREE = ROOT.parent / f"{ROOT.name}_gh_pages"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily retrain, export static Pages site, and optionally publish.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--today-config", default="configs/today_update.yaml")
    parser.add_argument("--universe-config", default="configs/universe_top50.yaml")
    parser.add_argument("--provider", default="fdr_poc")
    parser.add_argument("--target-date", default="latest")
    parser.add_argument("--output", default=str(DEFAULT_SITE_DIR))
    parser.add_argument("--pages-worktree", default=str(DEFAULT_WORKTREE))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="gh-pages")
    parser.add_argument("--publish", action="store_true", help="Commit and push the exported site to gh-pages.")
    parser.add_argument("--dry-run", action="store_true", help="Do not commit or push; run retrain in dry-run mode.")
    parser.add_argument("--skip-retrain", action="store_true", help="Export and publish currently stored DB snapshots.")
    parser.add_argument("--allow-empty", action="store_true", help="Allow preview/publish even when validation reports empty sections.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _load_dotenv(ROOT / ".env")
    run_result = _run_retrain(args)
    status = str(run_result.get("status") or "unknown")
    if not args.dry_run and not args.skip_retrain and status not in {"ready", "partial_ready"}:
        raise SystemExit(f"Retrain did not produce a publishable status: {status}")

    export_result = export_site(
        config_path=args.config,
        today_config_path=args.today_config,
        output_dir=args.output,
        run_status=status,
        run_result=run_result,
    )
    validation = _read_validation(Path(args.output))
    if args.publish and not args.allow_empty and not bool(validation.get("can_publish", False)):
        raise SystemExit("Static Pages validation failed; rerun with --allow-empty only for local preview/development.")
    publish_result = {"status": "skipped", "message": "pass --publish to update gh-pages"}
    if args.publish or args.dry_run:
        publish_result = publish_site(
            site_dir=Path(args.output),
            worktree=Path(args.pages_worktree),
            remote=args.remote,
            branch=args.branch,
            dry_run=args.dry_run,
            push=args.publish and not args.dry_run,
            allow_empty=args.allow_empty,
        )
    print(json.dumps({"run": run_result, "export": export_result, "publish": publish_result}, ensure_ascii=False, default=str))


def publish_site(
    *,
    site_dir: Path,
    worktree: Path,
    remote: str = "origin",
    branch: str = "gh-pages",
    dry_run: bool = False,
    push: bool = False,
    allow_empty: bool = False,
) -> dict[str, Any]:
    site_dir = site_dir.resolve()
    worktree = worktree.resolve()
    if not site_dir.exists():
        raise FileNotFoundError(site_dir)
    if worktree == ROOT.resolve():
        raise ValueError("pages worktree must not be the source repository root")
    files = sorted(str(path.relative_to(site_dir)) for path in site_dir.rglob("*") if path.is_file())
    if dry_run:
        return {
            "status": "dry_run",
            "would_publish_files": files[:200],
            "file_count": len(files),
            "pushed": False,
            "validation": _read_validation(site_dir),
        }

    validation = _read_validation(site_dir)
    if not allow_empty and not bool(validation.get("can_publish", False)):
        raise ValueError("Static Pages validation failed; publish blocked.")

    _ensure_pages_worktree(worktree, remote=remote, branch=branch)
    _sync_site(site_dir, worktree)
    status = _git(["status", "--short"], cwd=worktree).stdout.strip()
    if not status:
        return {"status": "no_changes", "pushed": False, "worktree": str(worktree)}

    _git(["add", "--all"], cwd=worktree)
    message = f"daily pages update {date.today().isoformat()} [skip ci]"
    _git(["commit", "-m", message], cwd=worktree)
    if push:
        _git(["push", remote, branch], cwd=worktree)
    return {
        "status": "published" if push else "committed",
        "message": message,
        "changed": status.splitlines(),
        "pushed": bool(push),
        "worktree": str(worktree),
    }


def _run_retrain(args: argparse.Namespace) -> dict[str, Any]:
    if args.skip_retrain:
        return {"status": "skipped", "message": "retrain skipped; exporting existing DB snapshots"}
    command = [
        sys.executable,
        "scripts/run_latest_market_impact_retrain.py",
        "--config",
        args.config,
        "--universe-config",
        args.universe_config,
        "--provider",
        args.provider,
        "--target-date",
        args.target_date,
    ]
    if args.dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return {
            "status": "failed",
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
        }
    parsed = _parse_runner_result(result.stdout)
    if args.dry_run:
        parsed.setdefault("status", "dry_run")
        parsed["dry_run_steps"] = [line for line in result.stdout.splitlines() if line.strip()]
    return parsed


def _parse_runner_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"status": "unknown", "stdout_tail": stdout[-2000:]}


def _ensure_pages_worktree(worktree: Path, *, remote: str, branch: str) -> None:
    if (worktree / ".git").exists():
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(["fetch", remote, branch], cwd=ROOT, check=False)
    remote_ref = f"{remote}/{branch}"
    exists = _git(["rev-parse", "--verify", remote_ref], cwd=ROOT, check=False).returncode == 0
    if exists:
        _git(["worktree", "add", "-B", branch, str(worktree), remote_ref], cwd=ROOT)
    else:
        _git(["worktree", "add", "-B", branch, str(worktree)], cwd=ROOT)


def _sync_site(site_dir: Path, worktree: Path) -> None:
    for child in worktree.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in site_dir.iterdir():
        target = worktree / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _read_validation(site_dir: Path) -> dict[str, Any]:
    path = site_dir / "data" / "validation.json"
    if not path.exists():
        return {"status": "failed", "can_publish": False, "errors": ["validation.json missing"]}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"status": "failed", "can_publish": False, "errors": [f"validation.json parse failed: {exc}"]}


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()
