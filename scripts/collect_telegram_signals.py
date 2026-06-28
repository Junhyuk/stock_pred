#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.db import append_dedup_table, connect_database
from roboquant.signals.telegram_signals import (
    build_telegram_market_signal_daily,
    build_telegram_signal_daily,
    normalize_telegram_message,
    render_daily_report,
    upsert_telegram_frames,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Telegram market/news signals.")
    parser.add_argument("--config", default="configs/telegram_signals.yaml")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--asof", default="latest")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _load_dotenv(ROOT / ".env")
    config = load_config(args.config)
    ensure_project_dirs(config)
    channels = list(config.get("channels", []))
    limit = _resolve_limit(args.limit, config)

    if args.dry_run:
        print(f"config: {Path(args.config)}")
        print(f"database: {get_database_path(config)}")
        print(f"report_dir: {config.get('paths', {}).get('report_dir')}")
        print(f"limit: {limit}")
        for channel in channels:
            print(
                "channel:",
                channel.get("username"),
                "source_weight=",
                channel.get("source_weight", 0.5),
            )
        return

    if not _telegram_credentials_available():
        print("TELEGRAM_API_ID/HASH not configured; skipped Telegram collection without fake data.")
        return

    conn = connect_database(get_database_path(config))
    try:
        posts, mentions = asyncio.run(_collect_all_channels(conn, channels, config, limit))
        upsert_telegram_frames(conn, posts, mentions)
        signals = build_telegram_signal_daily(conn, asof=args.asof, config=config)
        market_signals = build_telegram_market_signal_daily(conn, asof=args.asof, config=config)
        if not args.skip_report:
            report_path = _write_report(config, signals, args.asof)
            print(f"telegram report: {report_path}")
        print(f"telegram_posts rows upserted: {len(posts)}")
        print(f"telegram_ticker_mentions rows upserted: {len(mentions)}")
        print(f"telegram_signal_daily rows upserted: {len(signals)}")
        print(f"telegram_market_signal_daily rows upserted: {len(market_signals)}")
    finally:
        conn.close()


def _telegram_credentials_available() -> bool:
    return bool(os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"))


async def _collect_all_channels(conn, channels: list[dict[str, Any]], config: dict, limit: int):
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required for Telegram collection.")
    try:
        from telethon import TelegramClient
    except Exception as exc:
        raise RuntimeError("Telethon is required. Install with `python -m pip install -e '.[telegram]'`.") from exc

    session_path = _session_path(config)
    all_posts: list[dict[str, Any]] = []
    all_mentions: list[dict[str, Any]] = []
    async with TelegramClient(str(session_path), int(api_id), api_hash) as client:
        for channel in channels:
            try:
                posts, mentions = await _collect_channel(conn, client, channel, config, limit)
                all_posts.extend(posts)
                all_mentions.extend(mentions)
                print(f"OK: {channel.get('username')} posts={len(posts)} mentions={len(mentions)}")
            except Exception as exc:
                _record_failure(conn, channel, exc)
                print(f"FAIL: {channel.get('username')}: {exc}")
    return pd.DataFrame(all_posts), pd.DataFrame(all_mentions)


async def _collect_channel(conn, client, channel: dict[str, Any], config: dict, limit: int):
    username = str(channel["username"])
    source_weight = float(channel.get("source_weight", 0.5))
    await _ensure_channel_subscription(conn, client, channel)
    last_id = _last_message_id(conn, username)
    posts = []
    mentions = []
    async for message in client.iter_messages(username, limit=limit, min_id=last_id, reverse=True):
        post, mention_rows = normalize_telegram_message(
            channel=username,
            message_id=int(message.id),
            message_date=message.date,
            text=message.message or "",
            source_weight=source_weight,
            config=config,
        )
        posts.append(post)
        mentions.extend(mention_rows)
    return posts, mentions


async def _ensure_channel_subscription(conn, client, channel: dict[str, Any]) -> None:
    if channel.get("subscribe", True) is False:
        return
    username = str(channel["username"])
    try:
        from telethon.functions.channels import JoinChannelRequest

        await client(JoinChannelRequest(username))
    except Exception as exc:
        if exc.__class__.__name__ == "UserAlreadyParticipantError" or "already" in str(exc).lower():
            return
        _record_failure(conn, channel, RuntimeError(f"join failed before read: {exc}"))
        print(f"WARN: join failed for {username}; attempting public read: {exc}")


def _last_message_id(conn, channel: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(message_id), 0) FROM telegram_posts WHERE channel = ?",
        [channel],
    ).fetchone()
    return int(row[0] or 0)


def _write_report(config: dict, signals: pd.DataFrame, asof: str) -> Path:
    report_dir = Path(config.get("paths", {}).get("report_dir", ROOT / "reports" / "telegram_signals"))
    report_dir.mkdir(parents=True, exist_ok=True)
    top_n = int(config.get("ranking", {}).get("top_n", 10))
    report = render_daily_report(signals, asof=asof, top_n=top_n)
    report_path = report_dir / "daily_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def _record_failure(conn, channel: dict[str, Any], error: Exception) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(
            step="collect_telegram_signals",
            source=str(channel.get("username", "telegram")),
            target_date=datetime.now(UTC).date().isoformat(),
            error=error,
        ),
        ["collected_at", "step", "source", "error_message"],
    )


def _resolve_limit(arg_limit: int | None, config: dict) -> int:
    collection = config.get("collection", {})
    limit = int(arg_limit or collection.get("default_limit", 100))
    return max(1, min(limit, int(collection.get("max_messages_per_channel", 200))))


def _session_path(config: dict) -> Path:
    env_path = os.environ.get("TELEGRAM_SESSION_PATH")
    raw_path = env_path or config.get("collection", {}).get("session_path", "data/interim/telegram_stock_robo")
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
