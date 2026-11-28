import json
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

import pytest


def test_japan_cpi_reads_the_published_headline_change_and_preserves_gaps():
    from scripts.market_chart_macro import japan_cpi

    raw = (
        "類・品目,食料,総合\nGroup/Item,Food,All items\n"
        "類・品目符号(Group/Item code),0002,0001\n"
        "202603,5,1.5\n202604,6,\n202605,7,0.0\n202606,1,1.6\n"
    ).encode("cp932")
    assert japan_cpi(raw, date(2026, 3, 1), date(2026, 6, 1)) == {
        "2026-03-01": 1.5, "2026-05-01": 0.0,
    }


def test_eurostat_series_uses_category_positions_and_does_not_fill_gaps():
    from scripts.market_chart_macro import eurostat_points

    raw = {
        "id": ["freq", "geo", "time"],
        "size": [1, 1, 4],
        "dimension": {
            "freq": {"category": {"index": {"M": 0}}},
            "geo": {"category": {"index": {"EA21": 0}}},
            "time": {"category": {"index": {
                "2026-01": 2, "2026-02": 0, "2026-03": 1, "2026-04": 3,
            }}},
        },
        "value": {"0": 2.3, "1": 2.5, "3": 2.8},
    }
    assert eurostat_points(
        json.dumps(raw).encode(), {"freq": "M", "geo": "EA21"},
        date(2026, 1, 1), date(2026, 4, 1),
    ) == {"2026-02-01": 2.3, "2026-03-01": 2.5}
    with pytest.raises(ValueError):
        eurostat_points(
            json.dumps(raw).encode(), {"freq": "M", "geo": "EU27_2020"},
            date(2026, 1, 1), date(2026, 4, 1),
        )


def test_eurostat_release_metadata_uses_full_hicp_and_respects_cutoff():
    from scripts.market_chart_macro import eurostat_releases

    raw = [
        {"period": "October 2026", "start": "2026-11-04T11:00Z",
         "title": "Flash estimate inflation euro area", "euroindAuthor": "estat"},
        {"period": "October 2026", "start": "2026-11-18T11:00Z",
         "title": "Inflation (HICP)", "euroindAuthor": "estat"},
        {"period": "September 2026", "start": "2026-10-30T11:00Z",
         "title": "Unemployment", "euroindAuthor": "estat"},
        {"period": "October 2026", "start": "2026-12-02T11:00Z",
         "title": "Unemployment", "euroindAuthor": "estat"},
    ]
    releases = eurostat_releases(json.dumps(raw).encode(), datetime(2026, 11, 28, tzinfo=UTC))
    assert releases["ea_inflation"] == {
        "2026-10-01": ("2026-11-18", "https://ec.europa.eu/eurostat/web/products-euro-indicators/w/2-18112026-ap"),
    }
    assert set(releases["ea_unemployment"]) == {"2026-09-01"}
    day, url = releases["ea_unemployment"]["2026-09-01"]
    assert day == "2026-10-30"
    assert urlparse(url).path.endswith("/release-calendar")
    assert parse_qs(urlparse(url).query)["indicator"] == ["Unemployment"]


def test_bea_release_archive_distinguishes_combined_references_and_revisions():
    from scripts.market_chart_macro import bea_releases

    raw = b"""
    <table>
      <tr><td><a href="/news/2026/personal-income-and-outlays-october-and-november-2025">
        Personal Income and Outlays, October and November 2025</a></td>
        <td><time datetime="2026-01-22T08:30:00-05:00">January 22, 2026</time></td></tr>
      <tr><td><a href="/news/2025/personal-income-and-outlays-data-update-september-2025">
        Personal Income and Outlays, Data Update, September 2025</a></td>
        <td><time datetime="2025-12-23T08:30:00-05:00">December 23, 2025</time></td></tr>
      <tr><td><a href="/news/2025/personal-income-and-outlays-september-2025">
        Personal Income and Outlays, September 2025</a></td>
        <td><time datetime="2025-12-05T08:30:00-05:00">December 5, 2025</time></td></tr>
    </table>
    """
    releases = bea_releases(raw, datetime(2026, 1, 22, 14, tzinfo=UTC))
    assert releases["2025-09-01"][0] == "2025-12-05"
    assert releases["2025-10-01"] == releases["2025-11-01"]
    assert releases["2025-11-01"][0] == "2026-01-22"


def test_ecb_table_uses_deposit_rate_and_ignores_refinancing_only_changes():
    from scripts.market_chart_macro import ecb_policy_points

    raw = b"""
    <table><tr><th>Date (with effect from)</th><th></th><th>Deposit facility</th>
    <th>Main refinancing operations</th><th></th><th>Marginal lending facility</th></tr>
    <tr><td>2026</td><td>16 Sep.</td><td>2.50</td><td>2.65</td><td>-</td><td>2.90</td></tr>
    <tr><td>2026</td><td>18 Aug.<sup>5</sup></td><td>2.25</td><td>2.40</td><td>-</td><td>2.65</td></tr>
    <tr><td>2026</td><td>17 Jun.</td><td>2.25</td><td>2.45</td><td>-</td><td>2.65</td></tr></table>
    """
    assert ecb_policy_points(raw, date(2026, 11, 28)) == {
        "2026-06-17": 2.25, "2026-09-16": 2.50,
    }


def test_boj_target_bounds_are_separate_from_the_negative_balance_regime():
    from scripts.market_chart_macro import boj_policy

    def offline(_):
        raise OSError("offline")

    points, details, verified = boj_policy(date(2023, 11, 28), date(2026, 11, 28), offline)
    assert points["jp_negative_policy_balances"] == {"2016-02-16": -0.1, "2024-03-20": -0.1}
    assert verified == {"jp_negative_policy_balances": "2024-03-20"}
    assert points["jp_policy_rate_lower"]["2024-03-21"] == 0
    assert points["jp_policy_rate"]["2024-03-21"] == 0.05
    assert points["jp_policy_rate_upper"]["2024-03-21"] == 0.1
    assert points["jp_policy_rate"]["2026-09-24"] == 1.25
    assert details["jp_policy_rate"]["2026-09-24"][0] == "2026-09-18"


def test_pce_uses_each_same_month_index_and_preserves_comparison_gaps():
    from scripts.market_chart_macro import pce_points

    raw = b"""observation_date,PCEPI,PCEPILFE
2025-08-01,100,100
2025-09-01,101,
2026-08-01,103.3,102.2
2026-09-01,104.636,103
2026-10-01,105,104
"""
    read = pce_points(raw, date(2026, 8, 1), date(2026, 11, 1))
    assert read["us_pce"] == {"2026-08-01": 3.3, "2026-09-01": 3.6}
    assert read["us_core_pce"] == {"2026-08-01": 2.2}
