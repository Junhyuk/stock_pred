#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FORBIDDEN_STAGED_PREFIXES = (
    ".env",
    "data/",
    "models/",
    "reports/",
)


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh data, export GitHub Pages docs/, commit, and push."
    )
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--today-config", default="configs/today_update.yaml")
    parser.add_argument("--universe-config", default="configs/universe_top50.yaml")
    parser.add_argument("--market-news-config", default="configs/market_news.yaml")
    parser.add_argument("--provider", default="fdr_poc")
    parser.add_argument("--target-date", default="latest")
    parser.add_argument("--output", default="docs")
    parser.add_argument(
        "--refresh-mode",
        choices=["full", "light", "export-only"],
        default="light",
        help="full runs local Top50 retraining, light runs lightweight context refresh, export-only only rebuilds docs/.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-refresh", action="store_true", help="Backward-compatible alias for --refresh-mode export-only.")
    parser.add_argument("--skip-push", action="store_true", help="Validate local docs/ output without git add/commit/push.")
    parser.add_argument("--allow-empty", action="store_true", help="Allow publish even when validation.json is not publishable.")
    parser.add_argument("--no-stop-web", action="store_true", help="Do not stop local FastAPI/Streamlit/8787 before refresh.")
    parser.add_argument("--restart-web", action="store_true", help="Restart local FastAPI/Streamlit after completion.")
    parser.add_argument("--message", default="")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if args.skip_refresh:
        args.refresh_mode = "export-only"

    if args.dry_run:
        for step in build_steps(args, run_status=args.refresh_mode):
            print(_format_step(step))
        print("[dry-run] validate docs/data/validation.json")
        if args.skip_push:
            print("[dry-run] skip-push: no git add/commit/push")
        else:
            print("[dry-run] git add docs/ && git commit && git push origin HEAD")
        return

    run_status = _run_refresh(args)
    _run(export_step(args, run_status).command)
    validation = _read_validation(ROOT / args.output)
    if not args.allow_empty and not bool(validation.get("can_publish", False)):
        raise SystemExit("Static Pages validation failed; rerun with --allow-empty only for local preview/development.")

    if args.skip_push:
        print("skip-push: export/validation finished; no git changes were staged")
        if args.restart_web:
            _start_web()
        return

    _git_add_docs(ROOT / args.output)
    message = args.message.strip() or f"Publish GitHub Pages snapshot {date.today().isoformat()}"
    committed = _git_commit(message)
    if committed:
        _run(["git", "push", "origin", "HEAD"])
    else:
        print("nothing to commit; skipping push")
    if args.restart_web:
        _start_web()


def build_steps(args: argparse.Namespace, *, run_status: str = "manual") -> list[Step]:
    steps: list[Step] = []
    if not args.no_stop_web:
        steps.append(Step("stop_web", ["bash", "-lc", "fuser -k 8000/tcp 8501/tcp 8787/tcp >/dev/null 2>&1 || true"]))
    steps.extend(refresh_steps(args))
    steps.append(export_step(args, run_status))
    return steps


def refresh_steps(args: argparse.Namespace) -> list[Step]:
    if args.refresh_mode == "export-only":
        return []
    if args.refresh_mode == "full":
        return [
            Step(
                "full_local_retrain",
                [
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
                ],
            )
        ]
    return [
        Step(
            "collect_market_news",
            [
                sys.executable,
                "scripts/collect_market_news.py",
                "--config",
                args.market_news_config,
            ],
        ),
        Step(
            "today_market_update",
            [
                sys.executable,
                "scripts/run_today_market_update.py",
                "--config",
                args.today_config,
            ],
        ),
        Step(
            "generate_market_up_down",
            [
                sys.executable,
                "scripts/generate_market_up_down.py",
                "--config",
                args.config,
            ],
        ),
    ]


def export_step(args: argparse.Namespace, run_status: str) -> Step:
    return Step(
        "export_docs",
        [
            sys.executable,
            "scripts/export_github_pages_site.py",
            "--config",
            args.config,
            "--today-config",
            args.today_config,
            "--output",
            args.output,
            "--run-status",
            run_status,
        ],
    )


def _run_refresh(args: argparse.Namespace) -> str:
    status = args.refresh_mode
    for step in build_steps(args, run_status=args.refresh_mode)[:-1]:
        stdout = _run(step.command)
        if step.name == "full_local_retrain":
            parsed = _parse_runner_result(stdout)
            status = str(parsed.get("status") or status)
    return status


def _run(command: list[str]) -> str:
    printable = " ".join(command)
    print(f"$ {printable}")
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout).strip()
        raise SystemExit(f"command failed ({result.returncode}): {printable}\n{stderr[-2000:]}")
    return result.stdout


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


def _parse_runner_result(stdout: str) -> dict:
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
    return {"status": "unknown"}


def _read_validation(output_dir: Path) -> dict:
    path = output_dir / "data" / "validation.json"
    if not path.exists():
        return {"can_publish": False, "errors": ["validation.json missing"]}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"can_publish": False, "errors": [f"validation.json parse failed: {exc}"]}


def _format_step(step: Step) -> str:
    return f"{step.name}: {' '.join(step.command)}"


def _start_web() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    api_log = (log_dir / "fastapi.log").open("ab")
    streamlit_log = (log_dir / "streamlit.log").open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=ROOT,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app_streamlit.py",
            "--server.address",
            "0.0.0.0",
            "--server.port",
            "8501",
            "--server.headless",
            "true",
        ],
        cwd=ROOT,
        stdout=streamlit_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(2)
    print("FastAPI: http://localhost:8000/dashboard")
    print("Streamlit: http://localhost:8501")


if __name__ == "__main__":
    main()
