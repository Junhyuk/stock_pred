from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from roboquant.reports.prompt_templates import DISCLAIMER, validate_report_text
from roboquant.utils import write_json


def render_recommendations_markdown(recommendations: pd.DataFrame) -> str:
    if recommendations.empty:
        return f"# AI Robo Stock Recommendations\n\n추천 결과가 없습니다.\n\n> {DISCLAIMER}\n"

    asof_date = recommendations["asof_date"].iloc[0]
    horizon = recommendations["horizon"].iloc[0]
    lines = [
        "# AI Robo Stock Recommendations",
        "",
        f"- 기준일: `{asof_date}`",
        f"- Horizon: `{horizon}`",
        f"- 추천 수: `{len(recommendations)}`",
        "",
        "| Rank | Symbol | Name | Score | Reasons | Risk Flags |",
        "|---:|---|---|---:|---|---|",
    ]
    for _, row in recommendations.iterrows():
        reasons = ", ".join(json.loads(row.get("reason_json") or "[]"))
        risk_flags = ", ".join(json.loads(row.get("risk_flags_json") or "[]")) or "-"
        name = row.get("name") if pd.notna(row.get("name")) else ""
        lines.append(
            f"| {int(row['rank'])} | `{row['symbol']}` | {name} | {row['final_score']:.4f} | {reasons} | {risk_flags} |"
        )
    lines += ["", f"> {DISCLAIMER}", ""]
    return validate_report_text("\n".join(lines))


def render_recommendations_html(recommendations: pd.DataFrame) -> str:
    markdown = render_recommendations_markdown(recommendations)
    try:
        from jinja2 import Template

        rows = recommendations.to_dict(orient="records")
        template = Template(
            """
            <!doctype html>
            <html lang="ko">
            <head>
              <meta charset="utf-8" />
              <title>AI Robo Stock Recommendations</title>
              <style>
                body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }
                table { border-collapse: collapse; width: 100%; }
                th, td { border-bottom: 1px solid #d9e2ec; padding: 8px; text-align: left; }
                th { background: #f0f4f8; }
                .score { font-variant-numeric: tabular-nums; }
                .disclaimer { margin-top: 24px; color: #52606d; font-size: 14px; }
              </style>
            </head>
            <body>
              <h1>AI Robo Stock Recommendations</h1>
              {% if rows %}
              <p>기준일: <strong>{{ rows[0].asof_date }}</strong> / Horizon: <strong>{{ rows[0].horizon }}</strong></p>
              <table>
                <thead><tr><th>Rank</th><th>Symbol</th><th>Name</th><th>Score</th><th>Reasons</th><th>Risk Flags</th></tr></thead>
                <tbody>
                {% for row in rows %}
                  <tr>
                    <td>{{ row.rank }}</td>
                    <td><code>{{ row.symbol }}</code></td>
                    <td>{{ row.name | default("", true) }}</td>
                    <td class="score">{{ "%.4f"|format(row.final_score) }}</td>
                    <td>{{ row.reason_json }}</td>
                    <td>{{ row.risk_flags_json }}</td>
                  </tr>
                {% endfor %}
                </tbody>
              </table>
              {% else %}
              <p>추천 결과가 없습니다.</p>
              {% endif %}
              <p class="disclaimer">{{ disclaimer }}</p>
            </body>
            </html>
            """
        )
        return validate_report_text(template.render(rows=rows, disclaimer=DISCLAIMER))
    except Exception:
        return validate_report_text(f"<html><body><pre>{markdown}</pre></body></html>")


def render_backtest_html(curve: pd.DataFrame, summary: dict, horizon: str) -> str:
    if curve.empty:
        return _basic_html(f"<h1>Backtest {horizon}</h1><p>No backtest rows.</p>")

    try:
        import plotly.graph_objects as go

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=curve["asof_date"], y=curve["equity"], mode="lines", name="Top-K"))
        html_chart = fig.to_html(full_html=False, include_plotlyjs="cdn")
    except Exception:
        html_chart = ""

    summary_rows = "".join(
        f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in summary.items()
    )
    table = curve.tail(30).to_html(index=False)
    return _basic_html(
        f"""
        <h1>Backtest {horizon}</h1>
        {html_chart}
        <h2>Summary</h2>
        <table>{summary_rows}</table>
        <h2>Recent Periods</h2>
        {table}
        <p class="disclaimer">{DISCLAIMER}</p>
        """
    )


def render_backtest_comparison_html(comparison: pd.DataFrame, horizon: str) -> str:
    if comparison.empty:
        return _basic_html(f"<h1>Backtest Comparison {horizon}</h1><p>No comparison rows.</p>")
    table = comparison.to_html(index=False)
    return _basic_html(
        f"""
        <h1>Backtest Comparison {horizon}</h1>
        <p>Model, factor baseline, random baseline comparison for Top-K recommendations.</p>
        {table}
        <p class="disclaimer">{DISCLAIMER}</p>
        """
    )


def render_data_quality_markdown(
    prices: pd.DataFrame,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    market_metrics: pd.DataFrame,
    investor_flows: pd.DataFrame,
    failures: pd.DataFrame,
    warnings: list[str] | None = None,
) -> str:
    warnings = warnings or []
    lines = [
        "# Data Quality Report",
        "",
        f"- prices_daily rows: `{len(prices)}`",
        f"- features_daily rows: `{len(features)}`",
        f"- labels rows: `{len(labels)}`",
        f"- market_metrics_daily rows: `{len(market_metrics)}`",
        f"- investor_flows_daily rows: `{len(investor_flows)}`",
        f"- collection_failures rows: `{len(failures)}`",
        "",
        "## Coverage",
        "",
        f"- price symbols: `{prices['symbol'].nunique() if 'symbol' in prices else 0}`",
        f"- feature symbols: `{features['symbol'].nunique() if 'symbol' in features else 0}`",
        f"- market metric symbols: `{market_metrics['symbol'].nunique() if 'symbol' in market_metrics else 0}`",
        f"- investor flow symbols: `{investor_flows['symbol'].nunique() if 'symbol' in investor_flows else 0}`",
        "",
    ]
    if warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {warning}" for warning in warnings]
        lines.append("")
    if not failures.empty:
        lines += ["## Recent Collection Failures", ""]
        recent = failures.head(20)
        for _, row in recent.iterrows():
            lines.append(
                f"- `{row.get('collected_at')}` {row.get('step')} {row.get('symbol') or ''}: {row.get('error_message')}"
            )
        lines.append("")
    return validate_report_text("\n".join(lines))


def build_report_context(row: pd.Series) -> dict:
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "horizon": row.get("horizon"),
        "rank": _maybe_number(row.get("rank")),
        "final_score": _maybe_number(row.get("final_score")),
        "model_summary": {
            "model_version": row.get("model_version"),
            "pred_prob_top20": _maybe_number(row.get("pred_prob_top20")),
            "pred_return": _maybe_number(row.get("pred_return")),
            "confidence": _maybe_number(row.get("confidence")),
        },
        "positive_factors": _json_list(row.get("reason_json")),
        "risk_factors": _json_list(row.get("risk_flags_json")),
        "factor_scores": {
            "momentum_score": _maybe_number(row.get("momentum_score")),
            "value_score": _maybe_number(row.get("value_score")),
            "quality_score": _maybe_number(row.get("quality_score")),
            "supply_demand_score": _maybe_number(row.get("supply_demand_score")),
            "consensus_revision_score": _maybe_number(row.get("consensus_revision_score")),
            "target_upside_score": _maybe_number(row.get("target_upside_score")),
            "analyst_reliability_score": _maybe_number(row.get("analyst_reliability_score")),
            "consensus_upside_pct": _maybe_number(row.get("consensus_upside_pct")),
            "liquidity_score": _maybe_number(row.get("liquidity_score")),
            "risk_score": _maybe_number(row.get("risk_score")),
        },
        "disclaimer": DISCLAIMER,
    }


def write_report_contexts(recommendations: pd.DataFrame, root: str | Path) -> None:
    if recommendations.empty:
        return
    for _, row in recommendations.iterrows():
        asof_date = row.get("asof_date")
        horizon = row.get("horizon")
        symbol = row.get("symbol")
        path = Path(root) / "report_context" / str(asof_date) / str(horizon) / f"{symbol}.json"
        write_json(path, build_report_context(row))


def write_text(path: str | Path, content: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(validate_report_text(content), encoding="utf-8")


def _basic_html(body: str) -> str:
    return f"""
    <!doctype html>
    <html lang="ko">
    <head>
      <meta charset="utf-8" />
      <title>AI Robo Stock Report</title>
      <style>
        body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
        table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
        th, td {{ border-bottom: 1px solid #d9e2ec; padding: 8px; text-align: left; }}
        th {{ background: #f0f4f8; }}
        .disclaimer {{ margin-top: 24px; color: #52606d; font-size: 14px; }}
      </style>
    </head>
    <body>{body}</body>
    </html>
    """


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None or pd.isna(value):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [str(value)]
    return parsed if isinstance(parsed, list) else [parsed]


def _maybe_number(value):
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return value
