from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from roboquant.config import get_database_path, load_config
from roboquant.dashboard.dashboard_service import get_today_market_snapshot
from roboquant.dashboard.market_up_down_service import get_latest_market_up_down
from roboquant.db import connect_database
from roboquant.reports.prompt_templates import DISCLAIMER

PAGES_VERSION = "1"
PUBLIC_DISCLAIMER = (
    f"{DISCLAIMER} "
    "본 페이지는 연구·정보제공용 PoC 스냅샷이며 실시간 데이터가 아닙니다. "
    "투자 판단과 책임은 이용자 본인에게 있습니다."
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _kst_now_label() -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")


def _git_sha(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def collect_snapshots(
    conn,
    *,
    today_config: dict[str, Any],
) -> dict[str, Any]:
    today = get_today_market_snapshot(conn, today_config)
    up_down_2m = get_latest_market_up_down(conn, horizon="2M")
    up_down_6m = get_latest_market_up_down(conn, horizon="6M")
    return {
        "today": today,
        "up_down_2m": up_down_2m,
        "up_down_6m": up_down_6m,
    }


def _overall_status(snapshots: dict[str, Any]) -> str:
    today_status = str(snapshots["today"].get("status") or "not_collected")
    up_2m_count = len((snapshots["up_down_2m"].get("markets") or {}).get("KOSPI", {}).get("upside", []))
    if today_status in {"ready", "partial_ready"} and up_2m_count > 0:
        return "ready"
    if today_status in {"ready", "partial_ready"} or up_2m_count > 0:
        return "partial"
    return "partial"


def build_meta(*, root: Path, snapshots: dict[str, Any]) -> dict[str, Any]:
    today = snapshots["today"]
    return {
        "pages_version": PAGES_VERSION,
        "generated_at": _utcnow_iso(),
        "generated_at_kst": _kst_now_label(),
        "git_sha": _git_sha(root),
        "snapshot_date": today.get("snapshot_date"),
        "status": _overall_status(snapshots),
        "disclaimer": PUBLIC_DISCLAIMER,
        "pages": ["index.html", "up-down.html"],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _inline_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _page_shell(
    *,
    title: str,
    body: str,
    meta: dict[str, Any],
    inline_scripts: list[tuple[str, Any]],
    page_script: str,
) -> str:
    meta_banner = (
        f'<p class="notice public-banner">'
        f"PoC 스냅샷 · 갱신 {_escape_html(meta.get('generated_at_kst') or '-')}"
        f" · 기준일 {_escape_html(str(meta.get('snapshot_date') or '-'))}"
        f"</p>"
        f'<p class="notice">{_escape_html(meta.get("disclaimer") or PUBLIC_DISCLAIMER)}</p>'
    )
    script_tags = "\n".join(
        f'<script id="{script_id}" type="application/json">{_inline_json(payload)}</script>'
        for script_id, payload in inline_scripts
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_escape_html(title)}</title>
  <link rel="stylesheet" href="./assets/base.css" />
</head>
<body>
<main>
{meta_banner}
{body}
</main>
{script_tags}
<script src="./assets/site.js"></script>
<script src="./assets/{page_script}"></script>
</body>
</html>
"""


def _escape_html(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def render_index_html(*, meta: dict[str, Any], today: dict[str, Any]) -> str:
    body = """
<section class="hero compact">
  <a class="link" href="./up-down.html">상승·하락 Top10 →</a>
  <h1>오늘 시장 업데이트</h1>
  <p>국내 포커스 종목, 해외시장 동향, 글로벌 위험 레짐, 종목별 뉴스를 한 화면에서 확인합니다.</p>
</section>
<section class="grid cards" id="todayStatus">
  <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
</section>
<article class="panel">
  <h2>오늘·이번주 KOSPI/KOSDAQ 전망</h2>
  <div id="todayMarketOutlook" class="stack empty">시장 전망을 불러오는 중입니다.</div>
</article>
<article class="panel">
  <h2>오늘 급등락 원인 분석</h2>
  <div id="todayMoveExplanations" class="stack empty">원인 분석을 불러오는 중입니다.</div>
</article>
<article class="panel">
  <h2>KORU 레버리지 심리</h2>
  <div id="todayKoru" class="stack empty">KORU linkage를 불러오는 중입니다.</div>
</article>
<article class="panel">
  <h2>미국 유사섹터 영향</h2>
  <div id="todaySectorLinkage" class="stack empty">미국 유사섹터 linkage를 불러오는 중입니다.</div>
</article>
<section class="grid three">
  <article class="panel">
    <h2>국내 포커스 종목</h2>
    <div id="todayFocusPrices" class="table empty">국내 가격을 불러오는 중입니다.</div>
  </article>
  <article class="panel">
    <h2>Yahoo/yfinance 최신 가격</h2>
    <div id="todayYahooPrices" class="table empty">Yahoo 데이터를 불러오는 중입니다.</div>
  </article>
  <article class="panel">
    <h2>글로벌 위험 레짐</h2>
    <div id="todayRegime" class="stack empty">글로벌 레짐을 불러오는 중입니다.</div>
  </article>
</section>
<article class="panel">
  <h2>해외시장 동향</h2>
  <div id="todayGlobalMarkets" class="table empty">해외시장 데이터를 불러오는 중입니다.</div>
</article>
<article class="panel">
  <h2>종목별 최신 뉴스</h2>
  <div id="todayNews" class="stack empty">뉴스를 불러오는 중입니다.</div>
</article>
<p id="todayDisclaimer" class="notice"></p>
"""
    return _page_shell(
        title="AI Robo Quant Today Market Update",
        body=body,
        meta=meta,
        inline_scripts=[("today-snapshot", today)],
        page_script="today.js",
    )


def render_up_down_html(
    *,
    meta: dict[str, Any],
    up_down_2m: dict[str, Any],
    up_down_6m: dict[str, Any],
) -> str:
    body = """
<section class="hero compact">
  <a class="link" href="./index.html">← 오늘 시장 업데이트</a>
  <h1>Top50 시장별 상승·하락 추천</h1>
  <p>KOSPI/KOSDAQ 각각 상승 TOP6·4, 하락 TOP6·4를 시장 내 랭킹으로 선정합니다.</p>
  <div class="segmented" aria-label="예측 기간">
    <button type="button" class="active" data-horizon="2M" onclick="selectMarketUpDownHorizon(this)">2M 단기</button>
    <button type="button" data-horizon="6M" onclick="selectMarketUpDownHorizon(this)">6M 장기</button>
  </div>
</section>
<section id="marketUpDownMetrics" class="grid cards">
  <article class="metric"><span class="muted">로딩 중</span><strong>-</strong></article>
</section>
<section class="grid two">
  <article class="panel">
    <h2>KOSPI 상승 TOP6</h2>
    <div id="marketUpDownKospiUp" class="table empty">데이터를 불러오는 중입니다.</div>
  </article>
  <article class="panel">
    <h2>KOSPI 하락 TOP6</h2>
    <div id="marketUpDownKospiDown" class="table empty">데이터를 불러오는 중입니다.</div>
  </article>
</section>
<section class="grid two">
  <article class="panel">
    <h2>KOSDAQ 상승 TOP4</h2>
    <div id="marketUpDownKosdaqUp" class="table empty">데이터를 불러오는 중입니다.</div>
  </article>
  <article class="panel">
    <h2>KOSDAQ 하락 TOP4</h2>
    <div id="marketUpDownKosdaqDown" class="table empty">데이터를 불러오는 중입니다.</div>
  </article>
</section>
<p id="marketUpDownDisclaimer" class="notice"></p>
"""
    return _page_shell(
        title="AI Robo Quant Top50 Up-Down",
        body=body,
        meta=meta,
        inline_scripts=[
            ("up-down-2M", up_down_2m),
            ("up-down-6M", up_down_6m),
        ],
        page_script="up-down.js",
    )


def export_docs_bundle(
    *,
    root: Path,
    output_dir: Path,
    config_path: Path,
    today_config_path: Path,
) -> dict[str, Any]:
    config = load_config(config_path)
    today_config = load_config(today_config_path)
    conn = connect_database(get_database_path(config), read_only=True, initialize_schema=False)
    try:
        snapshots = collect_snapshots(conn, today_config=today_config)
    finally:
        conn.close()

    meta = build_meta(root=root, snapshots=snapshots)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    _write_json(data_dir / "today.json", snapshots["today"])
    _write_json(data_dir / "up-down-2M.json", snapshots["up_down_2m"])
    _write_json(data_dir / "up-down-6M.json", snapshots["up_down_6m"])
    _write_json(data_dir / "meta.json", meta)

    (output_dir / ".nojekyll").touch()
    (output_dir / "index.html").write_text(
        render_index_html(meta=meta, today=snapshots["today"]),
        encoding="utf-8",
    )
    (output_dir / "up-down.html").write_text(
        render_up_down_html(
            meta=meta,
            up_down_2m=snapshots["up_down_2m"],
            up_down_6m=snapshots["up_down_6m"],
        ),
        encoding="utf-8",
    )
    return {
        "meta": meta,
        "output_dir": str(output_dir),
        "files": [
            "index.html",
            "up-down.html",
            "data/today.json",
            "data/up-down-2M.json",
            "data/up-down-6M.json",
            "data/meta.json",
        ],
    }
