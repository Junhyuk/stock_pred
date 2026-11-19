from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from roboquant.index_forecast.official_credit import CreditSourceError, collect_credit_balances

HTML = """<html><body><dl><dt><a onclick="clickJisuMenu('OS0026')">신용융자</a></dt>
<dd><span class="dan">백만원</span> | <span class="date">11/18</span></dd>
<dd><span class="num1">26,266,366</span><span class="num2b">-67,489</span></dd>
</dl></body></html>"""


class Response:
    def __init__(self, body=HTML):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size=-1):
        return self.body.encode("utf-8")


def fixed_time():
    return datetime(2026, 11, 19, 13, 20, tzinfo=UTC)


def test_official_homepage_returns_only_global_balance_with_actual_receipt():
    with (
        patch("roboquant.index_forecast.official_credit.urlopen", return_value=Response()) as fetch,
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
    ):
        rows = collect_credit_balances(as_of=datetime(2026, 11, 19, 9, 13, tzinfo=UTC))
    assert len(rows) == 1
    row = rows[0]
    assert row["market"] == "GLOBAL"
    assert row["series"] == "MARGIN_CREDIT_BALANCE"
    assert row["observation_date"] == "2026-11-18"
    assert row["value"] == 26_266_366_000_000
    assert row["unit"] == "KRW"
    assert row["source"] == "kofia_freesis_margin_credit"
    assert row["received_at"] == "2026-11-19T13:20:00+00:00"
    assert row["published_at"] is None and row["is_final"] is None
    assert row["availability_policy"] == "observed"
    assert fetch.call_args.kwargs["timeout"] <= 15


@pytest.mark.parametrize(
    "html",
    [
        HTML.replace("백만원", "억원"),
        HTML.replace("26,266,366", "-"),
        HTML.replace("OS0026", "OS0027"),
        HTML + HTML,
    ],
)
def test_invalid_unverified_or_ambiguous_html_fails_closed(html):
    with (
        patch("roboquant.index_forecast.official_credit.urlopen", return_value=Response(html)),
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
    ):
        with pytest.raises(CreditSourceError):
            collect_credit_balances(as_of=fixed_time())


def test_prior_cutoff_must_not_relabel_current_snapshot_as_historical():
    with (
        patch("roboquant.index_forecast.official_credit.urlopen", return_value=Response()),
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
    ):
        with pytest.raises(CreditSourceError):
            collect_credit_balances(as_of=datetime(2025, 11, 19, tzinfo=UTC))


def test_year_rollover_uses_recent_prior_year_without_fabricating_future_date():
    january = datetime(2027, 1, 2, 8, 0, tzinfo=UTC)
    with (
        patch(
            "roboquant.index_forecast.official_credit.urlopen",
            return_value=Response(HTML.replace("11/18", "12/31")),
        ),
        patch("roboquant.index_forecast.official_credit._now_utc", return_value=january),
    ):
        rows = collect_credit_balances(as_of=january)
    assert rows[0]["observation_date"] == "2026-12-31"


def test_matching_official_api_values_are_reconciled_before_market_split():
    body = '{"response":{"header":{"resultCode":"00"},"body":{"totalCount":1,"items":{"item":[{"basDt":"20261118","crdTrFingWhl":"26266366","crdTrFingScrs":"20323672","crdTrFingKosdaq":"5942694"}]}}}}'
    with (
        patch(
            "roboquant.index_forecast.official_credit.urlopen",
            side_effect=[Response(), Response(body)],
        ),
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
    ):
        rows = collect_credit_balances(as_of=fixed_time(), api_key="test-key")
    by_market = {row["market"]: row for row in rows}
    assert set(by_market) == {"GLOBAL", "KOSPI", "KOSDAQ"}
    assert by_market["KOSPI"]["value"] == 20_323_672_000_000
    assert by_market["KOSDAQ"]["value"] == 5_942_694_000_000
    assert by_market["KOSPI"]["source"] == "fsc_kofia_margin_credit"
    assert all(row["published_at"] is None and row["is_final"] is None for row in rows)


def test_api_failure_or_mismatch_returns_global_with_warning_without_key_leak():
    body = '{"response":{"header":{"resultCode":"00"},"body":{"totalCount":1,"items":{"item":[{"basDt":"20261118","crdTrFingWhl":"26266365","crdTrFingScrs":"20323672","crdTrFingKosdaq":"5942694"}]}}}}'
    with (
        patch(
            "roboquant.index_forecast.official_credit.urlopen",
            side_effect=[Response(), Response(body)],
        ),
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
        pytest.warns(UserWarning) as warnings,
    ):
        rows = collect_credit_balances(as_of=fixed_time(), api_key="secret-test-key")
    assert [row["market"] for row in rows] == ["GLOBAL"]
    assert "secret-test-key" not in str(warnings[0].message)


@pytest.mark.parametrize(
    "changed",
    [
        {"basDt": "20261117"},
        {"crdTrFingWhl": "26266365"},
        {"crdTrFingKosdaq": "5942700"},
        {"crdTrFingScrs": "-1"},
    ],
)
def test_wrong_date_unit_sum_or_negative_balance_never_yields_market_rows(changed):
    import json

    item = {
        "basDt": "20261118",
        "crdTrFingWhl": "26266366",
        "crdTrFingScrs": "20323672",
        "crdTrFingKosdaq": "5942694",
    }
    item.update(changed)
    body = json.dumps(
        {
            "response": {
                "header": {"resultCode": "00"},
                "body": {"totalCount": 1, "items": {"item": [item]}},
            }
        }
    )
    with (
        patch(
            "roboquant.index_forecast.official_credit.urlopen",
            side_effect=[Response(), Response(body)],
        ),
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
        pytest.warns(UserWarning),
    ):
        rows = collect_credit_balances(as_of=fixed_time(), api_key="key")
    assert [row["market"] for row in rows] == ["GLOBAL"]


def test_api_network_failure_warning_does_not_expose_secret():
    secret = "SECRET-example-value"
    with (
        patch(
            "roboquant.index_forecast.official_credit.urlopen",
            side_effect=[Response(), RuntimeError(secret)],
        ),
        patch("roboquant.index_forecast.official_credit._now_utc", fixed_time),
        pytest.warns(UserWarning) as seen,
    ):
        rows = collect_credit_balances(as_of=fixed_time(), api_key=secret)
    assert [row["market"] for row in rows] == ["GLOBAL"]
    assert secret not in str(seen[0].message)
