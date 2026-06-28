from __future__ import annotations

import pytest

from roboquant.recommend.filters import build_exclusion_flags
from roboquant.reports.prompt_templates import DISCLAIMER, validate_report_text


def test_recommendation_exclusion_flags() -> None:
    row = {
        "trading_value_ma20": 100_000_000,
        "is_managed": True,
        "volatility_20d": 2.0,
        "credit_balance_change_20d": 0.5,
        "retail_overheat_score": 0.8,
    }

    flags = build_exclusion_flags(row)

    assert "low_liquidity" in flags
    assert "managed_stock" in flags
    assert "high_volatility" in flags
    assert "retail_credit_overheat" in flags


def test_report_banned_phrase_filter() -> None:
    with pytest.raises(ValueError):
        validate_report_text("이 종목은 무조건 상승합니다")


def test_disclaimer_mentions_analyst_targets_are_reference_only() -> None:
    assert "목표가와 애널리스트 의견은 보정된 참고 정보" in DISCLAIMER
