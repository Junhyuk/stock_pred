from datetime import UTC, datetime

import pandas as pd
import pytest
from test_refresh_premarket_prices import refresh

from roboquant.index_forecast.us_market_inputs import US_SYMBOLS, normalize_us_observations


def test_optional_morning_etfs_receive_equity_session_clock():
    raw = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "session_date": "2026-11-27",
                "close": 10,
                "source": "fixture",
                "source_url": "https://example.org/",
                "fetched_at": "2026-11-27T18:30Z",
            }
            for symbol in ("EWY", "KORU")
        ]
    )
    assert normalize_us_observations(raw)["observations"].empty
    rows = normalize_us_observations(raw, include_morning_etfs=True)["observations"]
    assert set(rows.symbol) == {"EWY", "KORU"}
    assert rows.session_close_at.eq("2026-11-27T18:00:00+00:00").all()
    assert rows.available_at.eq("2026-11-27T18:15:00+00:00").all()
    assert set(US_SYMBOLS) == {"^GSPC", "^IXIC", "^SOX", "^VIX"}


def test_new_koru_split_continues_frozen_price_scale_and_implied_return():
    cached = pd.DataFrame(
        [
            {"symbol": "KORU", "session_date": "2026-07-13", "close": 1.0},
            {"symbol": "KORU", "session_date": "2026-07-14", "close": 1.10},
        ]
    )
    changed = pd.DataFrame(
        {
            "session_date": ["2026-07-13", "2026-07-14", "2026-07-15"],
            "close": [20.0, 22.0, 20.9],
        }
    )
    appended = refresh.align_koru_bars(cached, changed)
    assert appended.close.tolist() == pytest.approx([1.0, 1.10, 1.045])
    assert appended.close.iloc[-1] / cached.close.iloc[-1] - 1 == pytest.approx(-0.05)
    assert cached.close.tolist() == [1.0, 1.10]


def test_koru_chart_rejects_unadjusted_reverse_split():
    timestamps = [
        int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())
        for value in ("2026-07-14T13:30:00", "2026-07-15T13:30:00")
    ]
    chart = {
        "meta": {"exchangeTimezoneName": "America/New_York"},
        "timestamp": timestamps,
        "indicators": {"quote": [{"close": [1.0, 19.5]}]},
        "events": {"splits": {"split": {"date": timestamps[1], "numerator": 1, "denominator": 20}}},
    }
    with pytest.raises(ValueError, match="split"):
        refresh.koru_chart_bars(chart, require_split=True)
    chart["indicators"]["quote"][0]["close"] = [20.0, 19.5]
    assert refresh.koru_chart_bars(chart, require_split=True).close.tolist() == [20.0, 19.5]


def test_morning_refresh_appends_etfs_without_replacing_ordinary_us_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "prices.sqlite"
    conn = refresh.connect_index_forecast(database)
    conn.execute(
        "INSERT INTO index_eod (index_code, session_date, close, source, ingested_at) "
        "VALUES ('KOSPI', '2026-11-25', 1000, 'fixture', '2026-11-25T09:00:00Z')"
    )
    conn.commit()
    conn.close()
    receipt = datetime(2026, 11, 26, 22, 55, tzinfo=UTC)
    old = tmp_path / "us_market_observations_v2_20261126.csv"
    raw = pd.DataFrame(
        [
            {
                "symbol": "^GSPC",
                "session_date": "2026-11-24",
                "close": 1200,
                "source": "fixture",
                "source_url": "https://example.org/",
                "fetched_at": "2026-11-25T22:00Z",
            }
        ]
    )
    refresh.write_us_observations(raw, old)
    old_bytes = old.read_bytes()

    class Yahoo:
        def download(self, symbol, **_kwargs):
            if symbol in ("^KS11", "^KQ11"):
                return pd.DataFrame()
            return pd.DataFrame(
                {"Close": [100, 101]}, index=pd.to_datetime(["2026-11-24", "2026-11-25"])
            )

    monkeypatch.setattr(
        refresh,
        "download_koru",
        lambda *_args, **_kwargs: pd.DataFrame(
            {"session_date": ["2026-11-24", "2026-11-25"], "close": [10, 10.1]}
        ),
    )
    result = refresh.refresh_premarket_prices(
        sqlite_path=database,
        output_dir=tmp_path,
        yf_module=Yahoo(),
        fetched_at=receipt,
        include_morning_etfs=True,
    )
    assert result["unavailable_morning_etfs"] == []
    assert old.read_bytes() == old_bytes
    rows = pd.read_csv(result["us_csv"])
    etfs = rows.loc[rows.symbol.isin(("EWY", "KORU"))]
    assert len(etfs) == 4
    assert etfs.first_seen_at.eq(receipt.isoformat()).all()
    base = rows.loc[rows.symbol.eq("^GSPC") & rows.session_date.eq("2026-11-24")]
    assert base.close.tolist() == [1200]
    assert base.first_seen_at.tolist() == ["2026-11-25T22:00:00+00:00"]
