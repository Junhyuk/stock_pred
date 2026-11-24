"""Publish B/C close intervals from an existing GPU research run."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from html import escape
from itertools import pairwise
from pathlib import Path

LEVEL = 0.80
WINDOW = 120
MINIMUM = 60
MARKETS = {"KOSDAQ": "코스닥", "KOSPI": "코스피"}
START = "<!-- model-bc-range:start -->"
END = "<!-- model-bc-range:end -->"


@dataclass(frozen=True)
class Row:
    origin: date
    target: date
    actual: float
    b_return: float
    c_probabilities: tuple[float, float, float]


@dataclass
class History:
    returns: list[float] = field(default_factory=list)
    b_errors: list[float] = field(default_factory=list)
    c_errors: list[float] = field(default_factory=list)


def event_class(value):
    if value <= -0.03 + 1e-12:
        return 0
    if value >= 0.03 - 1e-12:
        return 2
    return 1


def error_quantile(errors):
    sample = sorted(errors[-WINDOW:])
    rank = min(len(sample), math.ceil((len(sample) + 1) * LEVEL))
    return sample[rank - 1]


def b_band(expected_return, errors):
    margin = error_quantile(errors)
    return expected_return - margin, expected_return + margin


def c_band(returns, probabilities):
    """Allocate each predicted class's mass over its prior realized returns."""
    pools = [[], [], []]
    for value in returns:
        pools[event_class(value)].append(value)
    if not all(pools):
        return None
    total = sum(probabilities)
    if not math.isclose(total, 1.0, abs_tol=1e-6) or min(probabilities) < 0:
        raise ValueError("C class probabilities must be nonnegative and sum to one")
    weighted = []
    for pool, probability in zip(pools, probabilities, strict=True):
        sample = pool[-WINDOW:]
        weighted.extend((value, probability / (total * len(sample))) for value in sample)
    weighted.sort()
    quantiles = []
    mass = 0.0
    targets = [(1 - LEVEL) / 2, (1 + LEVEL) / 2]
    for value, weight in weighted:
        mass += weight
        while targets and mass >= targets[0] - 1e-12:
            targets.pop(0)
            quantiles.append(value)
        if not targets:
            return tuple(quantiles)
    raise ValueError("incomplete C probability distribution")


def calibrate(rows, asof, minimum=MINIMUM):
    history = History()
    coverage = {"n": 0, "b": 0, "c": 0}
    previous_target = None
    for row in rows:
        if row.origin >= row.target or (previous_target and previous_target > row.origin):
            raise ValueError("expected sequential next-session predictions")
        if row.target > asof:
            break
        base = c_band(history.returns, row.c_probabilities)
        if (
            row.target.year == asof.year
            and len(history.b_errors) >= minimum
            and len(history.c_errors) >= minimum
            and base is not None
        ):
            low_b, high_b = b_band(row.b_return, history.b_errors)
            margin = error_quantile(history.c_errors)
            coverage["n"] += 1
            coverage["b"] += low_b <= row.actual <= high_b
            coverage["c"] += base[0] - margin <= row.actual <= base[1] + margin
        if base is not None:
            history.c_errors.append(max(base[0] - row.actual, row.actual - base[1], 0.0))
        history.b_errors.append(abs(row.actual - row.b_return))
        history.returns.append(row.actual)
        previous_target = row.target
    return history, coverage


def load_rows(path):
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            yield Row(
                date.fromisoformat(row["session_date"]),
                date.fromisoformat(row["target_date"]),
                float(row["actual_return"]),
                float(row["model_close"]) / float(row["carry_close"]) - 1,
                tuple(float(row[f"model_c_{side}"]) for side in ("down", "neutral", "up")),
            )


def surge_context(rows: list[Row], asof: date) -> tuple[float, tuple[int, int, int]] | None:
    """Return the realized as-of surge and completed same-year outcomes after surges."""
    today = next((row.actual for row in reversed(rows) if row.target == asof), None)
    if today is None or event_class(today) != 2:
        return None
    outcomes = [0, 0, 0]
    for prior, following in pairwise(rows):
        if (
            following.target.year == asof.year
            and following.target <= asof
            and prior.target == following.origin
            and event_class(prior.actual) == 2
        ):
            outcomes[event_class(following.actual)] += 1
    return today, (outcomes[0], outcomes[1], outcomes[2])


def build_card(run_dir, market, forecast):
    asof = date.fromisoformat(forecast["asof_date"])
    target = date.fromisoformat(forecast["target_date"])
    if asof >= target:
        raise ValueError("forecast must target a session after its input close")
    rows = list(load_rows(run_dir / f"{market.lower()}_predictions.csv"))
    history, coverage = calibrate(rows, asof)
    if min(len(history.b_errors), len(history.c_errors)) < MINIMUM or not coverage["n"]:
        raise ValueError(f"{market} has insufficient realized forecasts for calibration")
    close = forecast["last_close"]
    bounds_b = b_band(forecast["expected_return"], history.b_errors)
    probabilities = tuple(forecast["c_probabilities"][side] for side in ("down", "neutral", "up"))
    base_c = c_band(history.returns, probabilities)
    margin = error_quantile(history.c_errors)
    bounds_c = (base_c[0] - margin, base_c[1] + margin)

    def points(bounds):
        low, high = (close * (1 + value) for value in bounds)
        return f"{low:,.2f}–{high:,.2f}"

    b_up = forecast["p_up"]
    c_down, c_neutral, c_up = probabilities
    b_coverage = coverage["b"] / coverage["n"]
    c_coverage = coverage["c"] / coverage["n"]
    surge = surge_context(rows, asof)
    realized_move = ""
    followup = ""
    if surge is not None:
        today, (down, neutral, up) = surge
        realized_move = f" · 전 거래일 대비 <strong>{today:+.2%}</strong> 급등"
        followup = (
            f'        <div class="detail-line">{asof.year}년 재구성 · 급등 다음 날 '
            f'{down + neutral + up}건 · 3% 이상 하락 {down}건 · 중립 {neutral}건 · '
            f'3% 이상 상승 {up}건</div>\n'
        )
    return f"""      <article class="forecast-card" aria-label="{MARKETS[market]} 종가 범위">
        <h3>{MARKETS[market]}</h3>
        <div class="card-dates">기준 종가 <strong>{close:,.2f}</strong>{realized_move}</div>
        <div class="hero-metric"><span>모델 B 종가 범위</span><strong>{points(bounds_b)}</strong></div>
        <div class="detail-line">예상 종가 {forecast["expected_close"]:,.2f} · {asof.year}년 범위 적중 {b_coverage:.1%}</div>
        <div class="prob-row">
          <div class="prob-box up"><span>B 상승 확률</span><strong>{b_up:.1%}</strong></div>
          <div class="prob-box down"><span>B 하락 확률</span><strong>{1 - b_up:.1%}</strong></div>
        </div>
        <div class="hero-metric"><span>모델 C 종가 범위</span><strong>{points(bounds_c)}</strong></div>
        <div class="detail-line">다음 거래일 C 예측 · 3% 이상 하락 {c_down:.2%} · 중립 {c_neutral:.2%} · 3% 이상 상승 {c_up:.2%}</div>
        <div class="detail-line">{asof.year}년 범위 적중 {c_coverage:.1%} · 공통 평가 {coverage["n"]}거래일</div>
{followup}      </article>"""


def publish(run_dir, page):
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    if report.get("production_weight") != 0 or not report.get("elapsed_seconds"):
        raise ValueError("expected a completed research run")
    forecasts = [report["markets"][market]["forecast"] for market in MARKETS]
    dates = {(forecast["asof_date"], forecast["target_date"]) for forecast in forecasts}
    if len(dates) != 1:
        raise ValueError("both markets must share forecast and target dates")
    (asof, target), = dates
    cards = "\n".join(
        build_card(run_dir, market, forecast)
        for market, forecast in zip(MARKETS, forecasts, strict=True)
    )
    section = f"""{START}
  <section id="model-bc-range" class="notice" aria-labelledby="model-bc-range-title" data-target-date="{escape(target)}">
    <h2 id="model-bc-range-title">모델 B·C 종가 범위 (연구용)</h2>
    <p>대상 거래일 <strong>{escape(target)}</strong> · 기준 종가일 {escape(asof)} KST · 가격 기반 재학습 · 명목 80% 범위</p>
    <div class="forecast-grid bc-range-grid">
{cards}
    </div>
    <p>모델 B는 최근 120건의 종가 예측 오차로 범위를 잡습니다. 모델 C는 급락·중립·급등 확률과 과거 수익률 분포를 결합하고 최근 120건의 오차로 보정합니다. 범위 적중률은 {escape(asof[:4])}년 과거 재구성 평가입니다.</p>
  </section>
  {END}"""
    text = page.read_text(encoding="utf-8")
    updated, count = re.subn(f"{re.escape(START)}.*?{re.escape(END)}", lambda _: section, text, flags=re.S)
    if count != 1:
        raise ValueError("page must contain exactly one B/C range section")
    page.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--page", type=Path, default=Path("docs/reliable-index-forecast.html"))
    args = parser.parse_args()
    publish(args.run_dir, args.page)
