from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from roboquant.config import get_database_path, load_config
from roboquant.dashboard.dashboard_service import (
    REGIME_SCORE_FIELDS,
    get_today_market_snapshot,
)
from roboquant.dashboard.price_gap_service import build_prediction_price_gap
from roboquant.db import connect_database
from roboquant.reports.prompt_templates import DISCLAIMER

ROOT = Path(__file__).resolve().parent
CONFIG = load_config(ROOT / "configs" / "poc.yaml")
TODAY_CONFIG = load_config(ROOT / "configs" / "today_update.yaml")


@st.cache_resource
def _conn():
    return connect_database(get_database_path(CONFIG), read_only=True, initialize_schema=False)


@st.cache_data(ttl=300)
def load_recommendations(horizon: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT
          r.*,
          s.name,
          s.market,
          p.pred_return,
          p.pred_prob_top20,
          p.confidence,
          f.consensus_upside_pct,
          f.consensus_revision_score,
          f.target_upside_score,
          f.analyst_reliability_score,
          f.weighted_analyst_reliability_score,
          f.new_coverage_count_30d,
          f.target_up_count_30d,
          f.target_down_count_30d
        FROM recommendations AS r
        LEFT JOIN symbols AS s ON r.symbol = s.symbol
        LEFT JOIN predictions AS p
          ON r.asof_date = p.asof_date
         AND r.symbol = p.symbol
         AND r.horizon = p.horizon
         AND r.model_version = p.model_version
        LEFT JOIN features_daily AS f
          ON r.asof_date = f.date
         AND r.symbol = f.symbol
         AND r.horizon = f.horizon
        WHERE r.horizon = ?
          AND r.asof_date = (SELECT MAX(asof_date) FROM recommendations WHERE horizon = ?)
        ORDER BY r.rank
        """,
        [horizon, horizon],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_prices(symbol: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM prices_daily
        WHERE symbol = ?
        ORDER BY date
        """,
        [symbol],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_latest_features(symbol: str, horizon: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT *
        FROM features_daily
        WHERE symbol = ?
          AND horizon = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol, horizon],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_consensus(symbol: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT *
        FROM consensus_history
        WHERE symbol = ?
        ORDER BY date DESC
        LIMIT 20
        """,
        [symbol],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_analyst_reports(symbol: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT
          report_date,
          broker_name,
          analyst_name,
          report_title,
          investment_rating,
          target_price,
          previous_target_price,
          target_change_pct,
          current_price_at_report,
          upside_pct_at_report,
          source_name,
          source_url
        FROM analyst_reports
        WHERE symbol = ?
        ORDER BY report_date DESC
        LIMIT 30
        """,
        [symbol],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_analyst_reliability(symbol: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT DISTINCT
          s.as_of_date,
          s.broker_name,
          s.analyst_name,
          s.report_count,
          s.recent_report_count_1y,
          s.direction_accuracy_12m,
          s.target_hit_rate_12m,
          s.mae_12m,
          s.reliability_score
        FROM analyst_reports AS r
        INNER JOIN analyst_scores AS s
          ON r.broker_name = s.broker_name
         AND r.analyst_name = s.analyst_name
        WHERE r.symbol = ?
        ORDER BY s.reliability_score DESC, s.as_of_date DESC
        LIMIT 20
        """,
        [symbol],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_model_registry() -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT
          model_name,
          model_type,
          feature_set_name,
          label_name,
          horizons,
          status,
          production_weight,
          shadow_mode,
          artifact_path,
          fail_reason,
          updated_at
        FROM model_registry
        ORDER BY updated_at DESC
        """
    ).fetchdf()


@st.cache_data(ttl=300)
def load_backtest_runs() -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT
          created_at,
          model_name,
          baseline_model_name,
          horizon,
          top_k,
          top20_return,
          excess_return,
          hit_ratio,
          mdd,
          turnover,
          sharpe,
          accepted,
          fail_reason
        FROM backtest_runs
        ORDER BY created_at DESC
        LIMIT 50
        """
    ).fetchdf()


@st.cache_data(ttl=300)
def load_shadow_predictions(horizon: str) -> pd.DataFrame:
    conn = _conn()
    return conn.execute(
        """
        SELECT p.*, s.name, s.market
        FROM model_predictions AS p
        LEFT JOIN symbols AS s ON p.symbol = s.symbol
        WHERE p.horizon = ?
          AND p.date = (
            SELECT MAX(date)
            FROM model_predictions
            WHERE horizon = ?
          )
        ORDER BY p.rank
        LIMIT 20
        """,
        [horizon, horizon],
    ).fetchdf()


@st.cache_data(ttl=300)
def load_dashboard_snapshot() -> dict:
    conn = _conn()
    snapshot = conn.execute(
        """
        SELECT *
        FROM dashboard_snapshot
        ORDER BY snapshot_date DESC
        LIMIT 1
        """
    ).fetchdf()
    if snapshot.empty:
        return {}
    row = snapshot.iloc[0]
    return {
        "snapshot_date": str(row.get("snapshot_date")),
        "position_summary": _json_value(row.get("position_summary_json"), {}),
        "theme_data": _json_value(row.get("theme_data_json"), {}),
        "ai_recommendations": _json_value(row.get("ai_recommendations_json"), []),
        "core_portfolio": _json_value(row.get("core_portfolio_json"), []),
        "quant_portfolio": _json_value(row.get("quant_portfolio_json"), {}),
        "qual_portfolio": _json_value(row.get("qual_portfolio_json"), {}),
        "upside_ranking": _json_value(row.get("upside_ranking_json"), []),
        "analyst_reports": _json_value(row.get("analyst_reports_json"), []),
        "backtest_summary": _json_value(row.get("backtest_summary_json"), {}),
        "model_accuracy": _json_value(row.get("model_accuracy_json"), []),
        "focus_stock": _json_value(row.get("focus_stock_json"), {}),
        "cluster_data": _json_value(row.get("cluster_data_json"), []),
        "sector_ranking": _json_value(row.get("sector_ranking_json"), []),
        "data_quality": _json_value(row.get("data_quality_json"), {}),
    }


@st.cache_data(ttl=120)
def load_today_market_snapshot() -> dict:
    conn = _conn()
    return get_today_market_snapshot(conn, TODAY_CONFIG)


@st.cache_data(ttl=120)
def load_price_gap_snapshot(horizon: str) -> dict:
    conn = _conn()
    return build_prediction_price_gap(
        conn,
        lookback_days=30,
        target_days=30,
        horizon=horizon,
        limit=300,
    )


def _json_value(value, default):
    if value is None or pd.isna(value):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _fmt_pct(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value) * 100:.1f}%"


def _metric_card(label: str, value: str, caption: str = "") -> None:
    st.markdown(
        f"""
        <div class="rq-card">
          <div class="rq-label">{label}</div>
          <div class="rq-value">{value}</div>
          <div class="rq-caption">{caption}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _short_date(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    return str(value)[:10]


def _render_global_regime(regime: dict) -> None:
    if not regime or regime.get("status") != "ready":
        st.info(regime.get("message") or "글로벌 레짐이 아직 없습니다.")
        return

    r1, r2, r3, r4 = st.columns(4)
    with r1:
        _metric_card("Regime", str(regime.get("regime") or "-"), _short_date(regime.get("prediction_date")))
    with r2:
        _metric_card("글로벌 위험도", f"{float(regime.get('global_risk_score') or 0):.0f}", "0=안정, 100=극단")
    with r3:
        cash_ratio = regime.get("recommended_cash_ratio")
        _metric_card("권장 현금비중", _fmt_pct(cash_ratio), "레짐 기준")
    with r4:
        futures_score = regime.get("futures_score")
        _metric_card("선물 리스크", f"{float(futures_score or 0):.0f}", "Nasdaq futures 기준")

    score_rows = []
    for field, label in REGIME_SCORE_FIELDS:
        value = regime.get(field)
        if value is None:
            continue
        score_rows.append({"구분": label, "점수": float(value)})
    if score_rows:
        st.write("위험 점수 구성")
        st.dataframe(pd.DataFrame(score_rows), use_container_width=True, hide_index=True)

    signals = regime.get("signals") or {}
    if signals:
        signal_rows = [
            {"신호": key, "값": float(value), "1D": _fmt_pct(value) if "return" in key else f"{float(value):.4f}"}
            for key, value in signals.items()
        ]
        st.write("핵심 신호")
        st.dataframe(pd.DataFrame(signal_rows), use_container_width=True, hide_index=True)

    reasons = regime.get("reasons") or []
    if reasons:
        st.write("판단 근거")
        for reason in reasons[:6]:
            st.markdown(f"- {reason}")


def _render_today_news(today_snapshot: dict, quality: dict) -> None:
    macro_news = today_snapshot.get("macro_news") or []
    if macro_news:
        st.write("거시·수급 뉴스")
        macro_frame = pd.DataFrame(macro_news)
        display_cols = [col for col in ["pub_date", "source", "category", "title", "link"] if col in macro_frame.columns]
        st.dataframe(macro_frame[display_cols], use_container_width=True, hide_index=True)

    news_frame = pd.DataFrame(today_snapshot.get("news") or [])
    if not news_frame.empty:
        st.write("종목별 뉴스")
        st.dataframe(news_frame[["pub_date", "symbol", "name", "title", "originallink"]], use_container_width=True)
        return

    messages = [message for message in quality.get("messages", []) if "뉴스" in str(message)]
    if messages and not macro_news:
        st.info(" · ".join(messages) + " · `.env`에 NAVER_CLIENT_ID/SECRET 설정 후 `scripts/run_today_market_update.py` 실행")
    elif messages and macro_news:
        st.caption(" · ".join(messages))
    elif not macro_news:
        st.info("수집된 뉴스가 없습니다.")

    context = today_snapshot.get("market_context") or []
    if context and not macro_news:
        st.write("시장 맥락 (뉴스 대체)")
        st.dataframe(pd.DataFrame(context), use_container_width=True, hide_index=True)


st.set_page_config(page_title="AI Robo Quant Lab", layout="wide")
st.markdown(
    """
    <style>
      .stApp { background: #302940; color: #ffffff; }
      [data-testid="stHeader"] { background: #302940; }
      .rq-title { font-size: 34px; font-weight: 800; color: #ffffff; margin: 6px 0 16px; }
      .rq-subtitle { color: #b5b7c4; font-size: 14px; margin-bottom: 18px; }
      .rq-card {
        background: #41465c;
        border: 1px solid #686d82;
        border-radius: 8px;
        padding: 16px;
        min-height: 112px;
      }
      .rq-label { color: #b5b7c4; font-size: 13px; }
      .rq-value { color: #ffffff; font-size: 26px; font-weight: 800; margin-top: 8px; }
      .rq-caption { color: #b5b7c4; font-size: 12px; margin-top: 8px; }
      .rq-chip { display: inline-block; background: #4d5268; border: 1px solid #686d82; padding: 4px 8px; border-radius: 6px; margin: 2px; color: #ffd96a; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown('<div class="rq-title">AI Robo Quant</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="rq-subtitle">추천주, 포트폴리오, 섹터, 예측 정확도, 백테스트 결과를 검증하는 로컬 대시보드입니다. '
    'FastAPI: <a href="http://localhost:8000/recommendations/up-down">상승·하락</a> · '
    '<a href="http://localhost:8000/recommendations/long-short">롱·숏</a> · '
    '<a href="http://localhost:8000/demo/today">Today Market</a></div>',
    unsafe_allow_html=True,
)

horizon = st.selectbox("Horizon", ["3M", "6M", "1Y", "2Y"], index=0)
snapshot = load_dashboard_snapshot()
recommendations = load_recommendations(horizon)
today_snapshot = load_today_market_snapshot()
price_gap_snapshot = load_price_gap_snapshot(horizon)

st.subheader("Today Market Update")
if not today_snapshot:
    st.info("오늘 업데이트 스냅샷이 없습니다. scripts/run_today_market_update.py를 실행하세요.")
else:
    quality = today_snapshot.get("data_quality", {})
    components = quality.get("components", {})
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        _metric_card("스냅샷", str(today_snapshot.get("status") or "-"), today_snapshot.get("snapshot_date", ""))
    with t2:
        _metric_card("국내 가격", components.get("domestic_prices", "-"), "")
    with t3:
        _metric_card("글로벌 레짐", components.get("market_regime", "-"), "")
    with t4:
        news_count = len(today_snapshot.get("news") or [])
        macro_count = len(today_snapshot.get("macro_news") or [])
        news_sub = f"종목 {news_count} · 거시 {macro_count}"
        if quality.get("messages"):
            news_sub = f"{news_sub} · {' · '.join(quality.get('messages', []))}"
        _metric_card("뉴스", components.get("news", "-"), news_sub)
    today_left, today_right = st.columns(2)
    with today_left:
        st.write("국내 포커스 종목")
        focus_frame = pd.DataFrame(today_snapshot.get("focus_prices", []))
        if focus_frame.empty:
            st.info("국내 포커스 종목 가격이 없습니다.")
        else:
            display_cols = [col for col in ["name", "symbol", "date", "close", "volume", "source", "status"] if col in focus_frame.columns]
            st.dataframe(focus_frame[display_cols], use_container_width=True, hide_index=True)
        st.write("글로벌 레짐")
        _render_global_regime(today_snapshot.get("global_regime", {}))
    with today_right:
        st.write("Yahoo/yfinance 최신 가격")
        yahoo_frame = pd.DataFrame(today_snapshot.get("yahoo_prices", []))
        if yahoo_frame.empty:
            st.info("Yahoo/yfinance 데이터가 없습니다. ALLOW_UNOFFICIAL_YAHOO=true 설정 후 업데이트 파이프라인을 실행하세요.")
        else:
            display_cols = [col for col in ["yahoo_symbol", "symbol", "asset_type", "date", "close", "currency", "source"] if col in yahoo_frame.columns]
            st.dataframe(yahoo_frame[display_cols], use_container_width=True)
        st.write("종목별 뉴스")
        _render_today_news(today_snapshot, quality)

st.subheader("Dashboard Snapshot")
if not snapshot:
    st.info("dashboard snapshot이 없습니다. scripts/build_dashboard_snapshot.py를 먼저 실행하세요.")
else:
    position = snapshot.get("position_summary", {})
    backtest = snapshot.get("backtest_summary", {})
    accuracy = snapshot.get("model_accuracy", [])
    focus = snapshot.get("focus_stock", {})
    focus_prediction = focus.get("prediction", {})
    focus_cluster = focus.get("cluster", {}).get("cluster") or {}
    st.write("삼성전자 포커스")
    f1, f2, f3, f4, f5 = st.columns(5)
    with f1:
        _metric_card("종목", focus.get("name") or "005930", focus.get("data_status", "데이터 미수집"))
    with f2:
        latest_price = focus.get("latest_price", {}).get("close")
        _metric_card("최근 종가", f"{latest_price:,.0f}" if latest_price else "-", "실제 수집 종가")
    with f3:
        _metric_card("3M 상승확률", _fmt_pct(focus_prediction.get("pred_prob_top20")), "전체 KOSPI 100 비교")
    with f4:
        rank = focus_prediction.get("rank")
        _metric_card("전체 순위", f"{int(rank)}위" if rank else "-", "Top20 강제 포함 없음")
    with f5:
        _metric_card("클러스터", focus_cluster.get("cluster_label", "-"), "KMeans 유사 종목군")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _metric_card("AI 로보 KOSPI", position.get("ai_robo", {}).get("kospi", "-"), "시장 방향 상태")
    with c2:
        _metric_card("Top20 승률", _fmt_pct(backtest.get("precision_top20")), "추천 상위 20개 실제 양수 비율")
    with c3:
        _metric_card("초과수익률", _fmt_pct(backtest.get("avg_excess_return")), "시장대비 평균 초과수익")
    with c4:
        _metric_card("Model Rows", str(len(accuracy)), "성능 요약 모델 수")

    left_snap, right_snap = st.columns([1.2, 1])
    with left_snap:
        st.write("AI 추천주")
        st.dataframe(pd.DataFrame(snapshot.get("ai_recommendations", [])), use_container_width=True)
    with right_snap:
        st.write("인기 섹터/테마")
        st.dataframe(pd.DataFrame(snapshot.get("theme_data", {}).get("sectors", [])), use_container_width=True)

    c5, c6 = st.columns(2)
    with c5:
        st.write("핵심 포트 종목")
        st.dataframe(pd.DataFrame(snapshot.get("core_portfolio", [])), use_container_width=True)
    with c6:
        st.write("정량 포트폴리오")
        portfolio = snapshot.get("quant_portfolio", {})
        st.dataframe(pd.DataFrame(portfolio.get("items", [])), use_container_width=True)

    c7, c8 = st.columns(2)
    with c7:
        st.write("Backtest 통계")
        st.json(backtest)
    with c8:
        st.write("모델 정확도")
        st.dataframe(pd.DataFrame(accuracy), use_container_width=True)

    c9, c10 = st.columns(2)
    with c9:
        st.write("산업별 추천")
        st.dataframe(pd.DataFrame(snapshot.get("sector_ranking", [])), use_container_width=True)
    with c10:
        st.write("종목 클러스터")
        st.dataframe(pd.DataFrame(snapshot.get("cluster_data", [])), use_container_width=True)
    with st.expander("데이터 품질"):
        st.json(snapshot.get("data_quality", {}))

st.subheader("Recent Prediction Price Gap")
price_gap_summary = price_gap_snapshot.get("summary", {})
g1, g2, g3, g4 = st.columns(4)
with g1:
    _metric_card("표본", str(price_gap_summary.get("sample_count", 0)), f"pending {price_gap_summary.get('pending_count', 0)}")
with g2:
    _metric_card("Latest MAE", _fmt_pct(price_gap_summary.get("mae_latest")), "현재까지 실제수익률 기준")
with g3:
    _metric_card("Latest Bias", _fmt_pct(price_gap_summary.get("bias_latest")), "양수면 과소예측")
with g4:
    _metric_card("방향 적중", _fmt_pct(price_gap_summary.get("direction_accuracy_latest")), "현재까지 방향")
price_gap_items = pd.DataFrame(price_gap_snapshot.get("items", []))
if price_gap_items.empty:
    st.info("최근 30일 예측 괴리 데이터가 없습니다.")
else:
    columns = [
        "prediction_date",
        "status",
        "symbol",
        "name",
        "horizon",
        "rank_no",
        "predicted_return",
        "actual_return_latest",
        "return_gap_latest",
        "actual_return_30d",
        "return_gap_30d",
    ]
    st.dataframe(price_gap_items[[column for column in columns if column in price_gap_items.columns]], use_container_width=True)

left, right = st.columns([2, 1])
with left:
    st.subheader(f"{horizon} Top Recommendations")
    if recommendations.empty:
        st.info("추천 결과가 없습니다. generate_recommendations.py를 먼저 실행하세요.")
    else:
        st.dataframe(recommendations, use_container_width=True)

with right:
    default_symbol = recommendations["symbol"].iloc[0] if not recommendations.empty else "005930"
    selected_symbol = st.text_input("Symbol", default_symbol)
    st.caption(DISCLAIMER)

prices = load_prices(selected_symbol)
features = load_latest_features(selected_symbol, horizon)

st.subheader(f"{selected_symbol} Chart")
fig = go.Figure()
if not prices.empty:
    fig.add_trace(
        go.Candlestick(
            x=prices["date"],
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            name="OHLC",
        )
    )
st.plotly_chart(fig, use_container_width=True)

st.subheader("Latest Factor Snapshot")
if features.empty:
    st.info("feature 데이터가 없습니다.")
else:
    factor_columns = [
        "momentum_score",
        "value_score",
        "quality_score",
        "supply_demand_score",
        "consensus_revision_score",
        "target_upside_score",
        "analyst_reliability_score",
        "weighted_analyst_reliability_score",
        "consensus_upside_pct",
        "target_up_count_30d",
        "target_down_count_30d",
        "new_coverage_count_30d",
        "liquidity_score",
        "risk_score",
        "foreign_net_value_20d_sum",
        "institution_net_value_20d_sum",
        "retail_net_value_20d_sum",
    ]
    available = [column for column in factor_columns if column in features.columns]
    st.dataframe(features[["date", "symbol", "horizon", *available]], use_container_width=True)

st.subheader("Recommendation Reasons")
if not recommendations.empty and selected_symbol in recommendations["symbol"].astype(str).tolist():
    row = recommendations[recommendations["symbol"].astype(str) == selected_symbol].iloc[0]
    reasons = json.loads(row.get("reason_json") or "[]")
    risks = json.loads(row.get("risk_flags_json") or "[]")
    st.write("Positive reasons")
    st.write(reasons)
    st.write("Risk flags")
    st.write(risks or ["-"])
else:
    st.info("선택한 종목이 최신 추천 목록에 없습니다.")

consensus = load_consensus(selected_symbol)
reports = load_analyst_reports(selected_symbol)
reliability = load_analyst_reliability(selected_symbol)

st.subheader("Analyst Consensus")
if consensus.empty:
    st.info("컨센서스 데이터가 없습니다.")
else:
    consensus_columns = [
        "date",
        "consensus_target_avg",
        "consensus_upside_pct",
        "consensus_momentum_30_90",
        "report_count_30d",
        "report_count_90d",
        "target_up_count_30d",
        "target_down_count_30d",
        "new_coverage_count_30d",
        "consensus_revision_score",
        "analyst_reliability_score",
    ]
    available = [column for column in consensus_columns if column in consensus.columns]
    st.dataframe(consensus[available], use_container_width=True)

st.subheader("Target Price History")
if reports.empty:
    st.info("애널리스트 리포트 import 데이터가 없습니다.")
else:
    st.dataframe(reports, use_container_width=True)

st.subheader("Analyst Reliability")
if reliability.empty:
    st.info("신뢰도 산출 조건을 만족한 애널리스트 점수가 없습니다.")
else:
    st.dataframe(reliability, use_container_width=True)

st.subheader("Model Gate")
st.caption("연구/정보제공용 모델 검증 화면입니다. DNN 모델은 Backtest Gate 통과 전까지 shadow mode로만 저장됩니다.")
registry = load_model_registry()
backtest_runs = load_backtest_runs()
shadow_predictions = load_shadow_predictions(horizon)

st.write("Model Registry")
if registry.empty:
    st.info("등록된 실험 모델이 없습니다.")
else:
    st.dataframe(registry, use_container_width=True)

st.write("Backtest Gate Runs")
if backtest_runs.empty:
    st.info("Backtest Gate 실행 결과가 없습니다.")
else:
    st.dataframe(backtest_runs, use_container_width=True)

st.write(f"{horizon} DNN Shadow Top20")
if shadow_predictions.empty:
    st.info("DNN shadow prediction 데이터가 없습니다.")
else:
    st.dataframe(shadow_predictions, use_container_width=True)
