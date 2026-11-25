import pytest

from scripts.collect_cfd_research import (
    discover_disclosures,
    parse_document,
    parse_pdf,
    retrospective_available_at,
)


def test_archive_requires_an_official_pdf_link_and_preserves_posted_date():
    layout = """<table>
      <tr><td>2</td>
        <td><span><a href="./view.do?seq=22&amp;page=1">종목별 CFD 잔고동향_20261124</a></span></td>
        <td><a href="./down.do?seq=22&amp;file_seq=1"><img src="/images/common/ext/pdf.gif"></a></td>
        <td>2026-11-25</td>
      </tr>
      <tr><td>1</td>
        <td><a href="./view.do?seq=21">종목별 CFD 잔고동향_20261123</a></td>
        <td><a href="https://external.example/down.do?seq=21"><img src="/images/common/ext/pdf.gif"></a></td>
        <td>2026-11-24</td>
      </tr>
    </table>"""
    result = discover_disclosures(layout, limit=1)
    assert len(result) == 1
    assert result[0].document_ref == "22"
    assert result[0].observation_date == "2026-11-24"
    assert result[0].posted_date == "2026-11-25"
    assert result[0].attachment_url == "https://www.kofia.or.kr/brd/m_198/down.do?seq=22&file_seq=1"
    with pytest.raises(ValueError, match="official attachment"):
        discover_disclosures(layout, limit=2)


def test_parse_pdf_rejects_html_before_invoking_poppler():
    with pytest.raises(ValueError, match="PDF"):
        parse_pdf(b"<!DOCTYPE html>", "2026-11-24")


def test_pdf_layout_preserves_alphanumeric_tickers_and_explicit_zero():
    layout = (
        ".          .               기준일자 :   2026-11-24\n"
        "종목코드  종목명     시장구분   잔고수량㈜   잔고금액(만원)\n"
        "0013R0  ETF 이름 (합성)  KOSPI   1,001  0\n"
        "001300  예시 회사  KOSDAQ   2  12,345\n\f"
    )
    items = parse_document(layout, "2026-11-24")
    assert [(item.ticker, item.name, item.market, item.quantity, item.value_krw) for item in items] == [
        ("0013R0", "ETF 이름 (합성)", "KOSPI", 1001, 0),
        ("001300", "예시 회사", "KOSDAQ", 2, 123450000),
    ]


@pytest.mark.parametrize("body", ["0013R0  예시  KOSPI  3  -", "0013R0  예시  KONEX  3  10"])
def test_unrecognized_line_cannot_enter_a_market_total(body):
    layout = "기준일자 : 2026-11-24\n종목코드 종목명 시장구분 잔고수량㈜ 잔고금액(만원)\n" + body
    with pytest.raises(ValueError, match="source row"):
        parse_document(layout, "2026-11-24")


def test_date_mismatch_cannot_be_relabelled_from_manifest():
    layout = (
        "기준일자 : 2026-11-23\n종목코드 종목명 시장구분 잔고수량㈜ 잔고금액(만원)\n"
        "001300  예시  KOSDAQ  10  1"
    )
    with pytest.raises(ValueError, match="observation date"):
        parse_document(layout, "2026-11-24")


def test_duplicate_ticker_cannot_be_summed_across_pages():
    page = (
        "기준일자 : 2026-11-24\n종목코드 종목명 시장구분 잔고수량㈜ 잔고금액(만원)\n"
        "001300  예시  KOSDAQ  10  1"
    )
    with pytest.raises(ValueError, match="duplicate ticker"):
        parse_document(f"{page}\f{page}", "2026-11-24")


def test_retrospective_availability_begins_at_next_korean_midnight():
    assert retrospective_available_at("2026-11-25") == "2026-11-25T15:00:00+00:00"
