import json

import pytest

from roboquant.index_forecast.automation_state import public_payload_paths
from roboquant.index_forecast.index_constituents import (
    collect,
    valid_payload,
)


def records(count, prefix):
    return [{"code": f"{prefix + i:06d}", "name": f"회사{i}"} for i in range(count)]


def market(day, count, prefix):
    return {"as_of": day, "count": count, "constituents": records(count, prefix)}


def test_collect_preserves_failed_market_and_never_regresses(tmp_path):
    path = tmp_path / "index_constituents.json"
    old = {"schema_version": 1, "indices": {"KOSPI200": market("2026-11-27", 201, 0), "KOSDAQ150": market("2026-11-27", 150, 300000)}}
    path.write_text(json.dumps(old), encoding="utf-8")
    def fetch(index, session):
        if index == "KOSPI200":
            return market(session, 202, 0)
        raise ValueError("unavailable")
    collect(path, "2026-11-30", fetch)
    result = json.loads(path.read_text())
    assert result["indices"]["KOSPI200"] == market("2026-11-30", 202, 0)
    assert result["indices"]["KOSDAQ150"] == old["indices"]["KOSDAQ150"]
    collect(path, "2026-11-30", lambda index, today: market("2026-11-26", 201 if index == "KOSPI200" else 150, 0 if index == "KOSPI200" else 300000))
    assert json.loads(path.read_text()) == result


def test_collect_rejects_partial_duplicate_or_malformed_data(tmp_path):
    path = tmp_path / "index_constituents.json"
    good = {"schema_version": 1, "indices": {"KOSPI200": market("2026-11-27", 201, 0), "KOSDAQ150": market("2026-11-27", 150, 300000)}}
    path.write_text(json.dumps(good), encoding="utf-8")
    for bad in [records(12, 0), records(200, 0) + [records(1, 0)[0]], records(200, 0) + [{"code": "123", "name": "x"}]]:
        collect(path, "2026-11-30", lambda index, day, rows=bad: {"as_of": day, "count": len(rows), "constituents": rows})
        assert json.loads(path.read_text()) == good


def test_public_payload_validation(tmp_path):
    path = tmp_path / "index_constituents.json"
    payload = {"schema_version": 1, "indices": {"KOSPI200": market("2026-11-27", 201, 0), "KOSDAQ150": market("2026-11-27", 150, 300000)}}
    assert valid_payload(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert path in public_payload_paths(tmp_path)
    payload["indices"]["KOSPI200"]["count"] = 200
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not valid_payload(payload)
    assert path not in public_payload_paths(tmp_path)


def test_parse_kis_security_master_preserves_date_and_index_membership():
    import io
    import zipfile

    from roboquant.index_forecast.index_constituents import parse_kis_zip

    lines = []
    for i in range(201):
        trailer = bytearray(b"0" * 227)
        trailer[18] = ord("1")
        code = "0009K0" if i == 0 else f"{i:06d}"
        lines.append(code.encode().ljust(9) + b" " * 12 + "삼성전자".encode("cp949") + trailer)
    trailer = bytearray(b"0" * 227)
    lines.extend([b"999999   " + b" " * 12 + "제외".encode("cp949") + trailer] * 300)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        info = zipfile.ZipInfo("kospi_code.mst", (2026, 11, 27, 7, 35, 2))
        archive.writestr(info, b"\n".join(lines))
    result = parse_kis_zip("KOSPI200", buf.getvalue(), "2026-11-28")
    assert result["as_of"] == "2026-11-27"
    assert result["count"] == 201
    assert {"code": "0009K0", "name": "삼성전자"} in result["constituents"]
    with pytest.raises(ValueError):
        parse_kis_zip("KOSPI200", buf.getvalue(), "2026-11-26")


def test_no_publication_until_both_markets_are_verified(tmp_path):
    path = tmp_path / "index_constituents.json"
    def fetch(index, today):
        if index == "KOSDAQ150":
            raise OSError("upstream unavailable")
        return market(today, 201, 0)
    collect(path, "2026-11-28", fetch)
    assert not path.exists()


def test_rejects_future_and_corrupt_existing_snapshot(tmp_path):
    path = tmp_path / "index_constituents.json"
    path.write_text('{"not": "a valid snapshot"}', encoding="utf-8")
    with pytest.raises(ValueError):
        collect(path, "2026-11-28", lambda *_: None)
    payload = {"schema_version": 1, "indices": {"KOSPI200": market("2026-11-29", 201, 0), "KOSDAQ150": market("2026-11-28", 150, 300000)}}
    assert not valid_payload(payload, today="2026-11-28")
